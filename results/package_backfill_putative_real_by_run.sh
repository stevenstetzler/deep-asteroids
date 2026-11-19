#!/usr/bin/env bash
# Per-run_id re-render of the putative-real PDF backfill (#32/#33), to pick up #38's viz
# changes (three coadd stacks, matched-trajectory overlay, grid lines, arcmin/hours units,
# thicker translucent arrow, legend fix) without waiting on package_backfill_putative_real.sh's
# own per-NIGHT sharding (~62 jobs, some nights taking hours) -- a job ARRAY instead, one task
# per run_id (one band's run_ids with >=1 putative-real result: $BAND, default VR -- the
# band='VR' searches #28 re-executed, which need `statistics`/`crossmatch --band VR`/`dedup --band VR --force` first;
# `BAND=none` re-renders the undefined-band legacy searches, 5,228 of them). Each task is far
# smaller (a run_id averages ~13 putative-real results) and uses $CORES (default 8) to fan its
# own result_nums out with xargs -P, so no single task dominates. $THROTTLE (default 50) caps
# how many tasks run at once, keeping this considerate of shared scheduler load rather than
# maximizing this job's own throughput.
#
# Same two-mode split, shard-snapshot, and $SUBMIT_BUDGET conventions as search_backfill_vr.sh
# (#28) -- see its own header comment for the full reasoning (Slurm's MaxArraySize caps an
# individual array job, not just a submission's total size; this cluster's ckpt QOS also caps
# MaxSubmitJobsPerUser=500 across every job this user has, not just this script's, so shards
# activate close to one at a time regardless; re-run this script as earlier shards drain).
#
# Usage (from a login node -- the submitter only materializes the run_id list and calls
# `sbatch`; array tasks are the only thing that actually renders):
#
#   DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/.../deep_asteroids_full_survey.sqlite \
#   DEEP_DB=/path/to/cached/deep_database.sqlite \
#   FORCE=1 results/package_backfill_putative_real_by_run.sh
#
# $BAND (default VR), $CORES (default 8), $THROTTLE (the array's `%N`, default 50), $SIGMA_V_THRESHOLD (default
# 0.00015, same as package_backfill_putative_real.sh), $PARTITION/$ACCOUNT (default
# ckpt-all/ckpt-astro), $TIME (per-task wall time, default 00:30:00 -- generous for ~13
# results/run_id at 8-way parallelism), $WEB_DIR (default ../web), $FORCE (unset by default;
# set to anything to pass --force through, re-rendering pages that already exist -- the whole
# point of this run) all override.
set -euo pipefail
# Slurm copies a submitted script into its own spool directory and runs that copy, so
# ${BASH_SOURCE[0]} resolves to the spool path on a worker, not this file's real location --
# $REPO_ROOT, exported by the submitter below, is how a worker finds its way back (same fix
# search_backfill_vr.sh needed).
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_ROOT"

: "${DEEP_ASTEROIDS_DB:?Set DEEP_ASTEROIDS_DB to the full-survey database}"
: "${DEEP_DB:?Set DEEP_DB to a cached deep-db copy}"
: "${SIGMA_V_THRESHOLD:=0.00015}"
: "${BAND:=VR}"  # which band's searches to render; none = the undefined-band legacy ones
: "${CORES:=8}"
: "${THROTTLE:=50}"
: "${PARTITION:=ckpt-all}"
: "${ACCOUNT:=ckpt-astro}"
: "${TIME:=00:30:00}"
: "${WEB_DIR:=$REPO_ROOT/web}"
: "${LOG_DIR:=$REPO_ROOT/results/logs}"
: "${FORCE:=}"
: "${RUN_ID_LIST:=$REPO_ROOT/results/putative_real_run_ids.txt}"
# Only what a worker actually needs -- plain (non-exported) shell variables don't survive
# into an sbatch job's environment even with --export=ALL.
export DEEP_ASTEROIDS_DB DEEP_DB SIGMA_V_THRESHOLD BAND WEB_DIR FORCE REPO_ROOT RUN_ID_LIST

if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
    # Worker: one array task, one run_id -- line $SLURM_ARRAY_TASK_ID of $RUN_ID_LIST, which
    # for a worker is always a per-shard *snapshot* file the submitter wrote once and never
    # touches again, never the submitter's own shared, repeatedly-overwritten candidate list
    # (see search_backfill_vr.sh's own comment on the race this avoids).
    run_id=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$RUN_ID_LIST")
    [[ -n "$run_id" ]] || {
        echo "package_backfill_putative_real_by_run.sh: no run_id at line $SLURM_ARRAY_TASK_ID of $RUN_ID_LIST" >&2
        exit 1
    }
    echo "package_backfill_putative_real_by_run.sh: task $SLURM_ARRAY_TASK_ID: $run_id, job $SLURM_JOB_ID, restart count ${SLURM_RESTART_COUNT:-0}" >&2

    result_nums=$(deep-asteroids query results --run-id "$run_id" --putative-real \
        --sigma-v-threshold "$SIGMA_V_THRESHOLD" --header 2>/dev/null \
        | tail -n +2 | awk -F'|' '{print $2}' | sort -un || true)
    n_results=$(grep -c . <<< "$result_nums" || true)
    echo "package_backfill_putative_real_by_run.sh: $run_id: $n_results putative-real result(s), $SLURM_CPUS_PER_TASK worker(s) (\$SLURM_CPUS_PER_TASK)" >&2

    if [[ "$n_results" -gt 0 ]]; then
        xargs -P "$SLURM_CPUS_PER_TASK" -I{} \
            deep-asteroids package result --run-id "$run_id" --result-num {} --putative-real --no-movie \
                --sigma-v-threshold "$SIGMA_V_THRESHOLD" --web-dir "$WEB_DIR" ${FORCE:+--force} <<< "$result_nums"
    fi
    echo "package_backfill_putative_real_by_run.sh: $run_id done" >&2
    exit 0
fi

# Submitter: materialize the candidate list fresh (idempotent -- straight from the database's
# current state), then submit one array job per shard, each against its *own* snapshot file.
#
# list_putative_real_run_ids.py has no "already done" exclusion of its own (unlike
# list_vr_run_ids.py, which drops any run_id with a committed `search` row) -- re-rendering
# existing pages is the whole point here, --force included, so "already has a page" is never
# a reason to skip a run_id. $SUBMITTED_LIST is purely this script's own bookkeeping, so a
# budget-limited resubmission doesn't keep re-offering a run_id whose shard is already queued
# or running.
: "${SUBMITTED_LIST:=$REPO_ROOT/results/putative_real_run_ids_submitted.txt}"
CANDIDATES="$REPO_ROOT/results/putative_real_run_ids.txt"
mkdir -p "$LOG_DIR" "$(dirname "$CANDIDATES")"
touch "$SUBMITTED_LIST"
python3 results/list_putative_real_run_ids.py --sigma-v-threshold "$SIGMA_V_THRESHOLD" --band "$BAND" \
    | grep -vFxf "$SUBMITTED_LIST" > "$CANDIDATES" || true
n=$(wc -l < "$CANDIDATES")
if [[ "$n" -eq 0 ]]; then
    echo "package_backfill_putative_real_by_run.sh: nothing left to submit (already queued or already submitted this run -- clear \$SUBMITTED_LIST to start over)" >&2
    exit 0
fi

: "${MAX_ARRAY_SIZE:=400}"
: "${SUBMIT_BUDGET:=450}"
n_shards=$(( (n + MAX_ARRAY_SIZE - 1) / MAX_ARRAY_SIZE ))
echo "package_backfill_putative_real_by_run.sh: $n run_id(s) in $CANDIDATES, $n_shards array job(s) needed, $THROTTLE concurrent per active shard, $CORES core(s)/task" >&2

offset=0
while [[ "$offset" -lt "$n" ]]; do
    chunk=$(( n - offset < MAX_ARRAY_SIZE ? n - offset : MAX_ARRAY_SIZE ))

    # -r: expand a pending array job into one row per task -- squeue's default collapsed
    # display undercounts by exactly the array size (confirmed the hard way in #28).
    current_jobs=$(squeue -u "$USER" -h -r | wc -l)
    if [[ "$((current_jobs + chunk))" -gt "$SUBMIT_BUDGET" ]]; then
        remaining=$(( n - offset ))
        echo "package_backfill_putative_real_by_run.sh: stopping -- $current_jobs job(s) already queued, next shard ($chunk tasks) would exceed \$SUBMIT_BUDGET ($SUBMIT_BUDGET). $remaining run_id(s) still left; re-run this script once earlier shards have drained." >&2
        break
    fi

    shard_list="$REPO_ROOT/results/putative_real_run_ids.shard_$(date +%s%N)_${offset}.txt"
    sed -n "$((offset + 1)),$((offset + chunk))p" "$CANDIDATES" > "$shard_list"

    job_id=$(sbatch --parsable -p "$PARTITION" -A "$ACCOUNT" -c "$CORES" --time "$TIME" --requeue \
        --array="1-${chunk}%${THROTTLE}" \
        --job-name "package-backfill-by-run" \
        --output "$LOG_DIR/package_backfill_by_run_%A_%a.out" --open-mode=append \
        --export="ALL,RUN_ID_LIST=${shard_list}" \
        "$REPO_ROOT/results/package_backfill_putative_real_by_run.sh")
    echo "package_backfill_putative_real_by_run.sh: submitted array job $job_id (lines $((offset + 1))-$((offset + chunk)) of $CANDIDATES, snapshot $shard_list, throttle $THROTTLE)" >&2
    cat "$shard_list" >> "$SUBMITTED_LIST"
    offset=$(( offset + chunk ))
done
