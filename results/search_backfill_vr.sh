#!/usr/bin/env bash
# #28's re-execution of the full survey's searches, pinned to band='VR' (see
# docs/plans/PLAN_28_SEARCH_VR_REEXECUTION.md). A Slurm job ARRAY, not the per-night
# xargs-fanout shape package_backfill_putative_real.sh (#32/#33) uses -- `search run` is
# written to own its *whole* job's CPU budget per run_id
# (numba.set_num_threads(allocated_cpus()), detect_exposures()'s own worker pool), unlike
# `package result`'s single-threaded-per-call design, so fanning several run_ids out inside
# one job would oversubscribe badly. One array task per run_id instead; $THROTTLE (the
# array's `%N`) bounds how many run at once -- Slurm-allocation-honest, and also bounds how
# many concurrent SQLite writers the busy-timeout fix (#37) actually has to absorb.
#
# Calibrated against 12 real in-scope VR search definitions (10 succeeded): ~51.6s wall per
# search at 4 CPUs (detect ~0.46s/exposure, search ~7.6s roughly flat, ingest ~0.02s/exposure;
# 798 total exposures across the 10). At $THROTTLE=16, $CORES=4 over the 4,261 run_ids
# list_vr_run_ids.py selects: ~3.8h wall, ~244 core-hours total -- both well short of the PDF
# backfill's (#32/#33) scale. The other 2 of 12 calibration runs failed with "no
# differenceExp image ... run `ingest images` first" -- a real data gap (not every planned
# detector-exposure was actually observed/calibrated that night), not a bug;
# list_vr_run_ids.py already excludes every run_id with any such gap (1,311 of 5,572,
# checked precisely, not estimated from the sample).
#
# Needs the vendored detect engine built (src/deep_asteroids/util/detect_engine/build.sh)
# and the Butler registry reachable: source /gscratch/dirac/DEEP/data/credentials first.
#
# Usage (from a login node -- the submitter only materializes the run_id list and calls
# `sbatch`; array tasks are the only thing that actually searches):
#
#   DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/.../deep_asteroids_full_survey.sqlite \
#   DEEP_DB=/path/to/cached/deep_database.sqlite \
#   source /gscratch/dirac/DEEP/data/credentials \
#   results/search_backfill_vr.sh
#
# $CORES (default 4, per calibration), $THROTTLE (the array's `%N`, default 16),
# $PARTITION/$ACCOUNT (default ckpt-all/ckpt-astro), $TIME (per-task wall time, default
# 00:30:00 -- the isolated calibration's slowest run was ~77s, but real concurrent execution
# on a shared, contended node hit one real detect() taking 552.6s and killed two more at the
# original 10-minute limit; 30 minutes is a guess at enough headroom, not itself calibrated),
# $CATALOG_DIR (default /gscratch/scrubbed/$USER/deep_asteroids/catalogs -- scrubbed, so
# don't let this run span the ~21-day inactivity window) all override.
#
# ckpt-all/ckpt-astro is preemptible -- `--requeue` is passed explicitly (on top of the
# partition's own PreemptMode=REQUEUE), `--open-mode=append` keeps a requeued task's log
# instead of wiping it, and each task logs its own restart count. `run_search_for_definition`
# (#37) already skips a run_id with an existing `search` row unless --force, and now commits
# its results and its own SearchRun provenance row atomically in one transaction -- so a
# requeued or resubmitted task is always either fully done or not started at all, never
# missing just its SearchRun row the way an interrupted run used to be able to.
set -euo pipefail
# Slurm copies a submitted script into its own spool directory and runs that copy, so
# ${BASH_SOURCE[0]} resolves to the spool path on a worker (e.g.
# /var/spool/slurmd/job<N>/slurm_script), not this file's real location -- confirmed the hard
# way (a worker's relative-path defaults silently resolved under /var/spool/slurmd and
# failed). $REPO_ROOT, exported by the submitter below, is how a worker finds its way back; a
# human running this directly (the submitter case) computes it the normal way instead.
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_ROOT"

: "${DEEP_ASTEROIDS_DB:?Set DEEP_ASTEROIDS_DB to the full-survey database}"
: "${DEEP_DB:?Set DEEP_DB to a cached deep-db copy}"
: "${CORES:=4}"
: "${THROTTLE:=16}"
: "${PARTITION:=ckpt-all}"
: "${ACCOUNT:=ckpt-astro}"
: "${TIME:=00:30:00}"
: "${CATALOG_DIR:=/gscratch/scrubbed/$USER/deep_asteroids/catalogs}"
: "${LOG_DIR:=$REPO_ROOT/results/logs}"
: "${RUN_ID_LIST:=$REPO_ROOT/results/vr_run_ids.txt}"
: "${SUBMITTED_LIST:=$REPO_ROOT/results/vr_run_ids_submitted.txt}"
# Only what a worker actually needs -- plain (non-exported) shell variables don't survive
# into an sbatch job's environment even with --export=ALL, which passes the *process*
# environment, not this shell's local variables (another one hit the hard way: the worker
# saw none of these until they were exported here).
export DEEP_ASTEROIDS_DB DEEP_DB REPO_ROOT RUN_ID_LIST CATALOG_DIR

if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
    # Worker: one array task, one run_id -- line ($CHUNK_OFFSET + $SLURM_ARRAY_TASK_ID) of
    # $RUN_ID_LIST. $CHUNK_OFFSET (default 0) lets the submitter shard a run_id list longer
    # than Slurm's own MaxArraySize (1001 on this cluster -- an individual task ID, not just
    # the range size, must stay below it) across several array jobs that all read the same
    # list, each covering a different chunk.
    line=$(( ${CHUNK_OFFSET:-0} + SLURM_ARRAY_TASK_ID ))
    run_id=$(sed -n "${line}p" "$RUN_ID_LIST")
    [[ -n "$run_id" ]] || {
        echo "search_backfill_vr.sh: no run_id at line $line of $RUN_ID_LIST" >&2
        exit 1
    }
    echo "search_backfill_vr.sh: task $SLURM_ARRAY_TASK_ID (line $line): $run_id, job $SLURM_JOB_ID, restart count ${SLURM_RESTART_COUNT:-0}" >&2

    deep-asteroids search run --run-id "$run_id" --catalog-dir "$CATALOG_DIR" --remove-catalogs
    echo "search_backfill_vr.sh: task $SLURM_ARRAY_TASK_ID: $run_id done" >&2
    exit 0
fi

# Submitter: materialize the run_id list (idempotent -- always recomputed fresh from the
# database's current state), then submit one array job over it.
#
# list_vr_run_ids.py only excludes a run_id once it has a *committed* `search` row -- which a
# shard submitted by an earlier invocation of this script won't have yet while its tasks are
# still queued/running. Without tracking that separately, a budget-limited resubmission (the
# whole point of $SUBMIT_BUDGET below) would keep re-offering the same still-in-flight run_ids
# as the "next" chunk. $SUBMITTED_LIST is this script's own flat-file manifest of every run_id
# it has ever submitted (appended to as each shard goes out, never cleared) -- not a new
# database table, just enough bookkeeping for this one script to stay resumable.
: "${SUBMITTED_LIST:=$(pwd)/results/vr_run_ids_submitted.txt}"
mkdir -p "$LOG_DIR" "$(dirname "$RUN_ID_LIST")"
touch "$SUBMITTED_LIST"
python3 results/list_vr_run_ids.py | grep -vFxf "$SUBMITTED_LIST" > "$RUN_ID_LIST" || true
n=$(wc -l < "$RUN_ID_LIST")
if [[ "$n" -eq 0 ]]; then
    echo "search_backfill_vr.sh: nothing left to submit (already done or already in flight)" >&2
    exit 0
fi
# Shard into chunks of at most $MAX_ARRAY_SIZE (default 400): Slurm's own MaxArraySize
# (1001 on this cluster) caps an individual task ID, not just the range size, so a list this
# long needs more than one array job anyway -- but the real reason it's smaller than that cap
# is the next one.
#
# A second, much tighter limit this cluster's `ckpt` QOS also enforces --
# MaxSubmitJobsPerUser=500, every PENDING+RUNNING job this user has anywhere, not just this
# script's -- means an entire shard's tasks sit counted against that budget for as long as any
# of them are still pending or running, however many are actually executing at once under
# $THROTTLE. In practice that makes shards activate close to one at a time regardless of how
# this script's own logic is written, so $THROTTLE is used as-is per shard (not divided down
# by the shard count) -- dividing it further would just slow down the one shard actually
# running for no reason. $SUBMIT_BUDGET (default 450, leaving headroom under 500 for other
# jobs) caps how many of this user's total jobs this invocation will push toward; it submits
# shards one at a time, stops (without erroring) the moment the next one wouldn't fit, and
# says how much is left. Expect to re-run this script repeatedly as earlier shards drain --
# between list_vr_run_ids.py's own exclusion (anything with a `search` row) and
# $SUBMITTED_LIST (anything this script has already queued, committed or not), each re-run
# only enumerates the actual remainder.
: "${MAX_ARRAY_SIZE:=400}"
: "${SUBMIT_BUDGET:=450}"
n_shards=$(( (n + MAX_ARRAY_SIZE - 1) / MAX_ARRAY_SIZE ))
echo "search_backfill_vr.sh: $n run_id(s) in $RUN_ID_LIST, $n_shards array job(s) needed, $THROTTLE concurrent per active shard" >&2

offset=0
while [[ "$offset" -lt "$n" ]]; do
    chunk=$(( n - offset < MAX_ARRAY_SIZE ? n - offset : MAX_ARRAY_SIZE ))

    # -r: expand a pending array job into one row per task -- squeue's default collapsed
    # "<jobid>_[1-400]" display undercounts by exactly the array size, since every task still
    # counts individually against MaxSubmitJobsPerUser regardless of how squeue displays it
    # (confirmed directly: submitting one 400-task shard took the real count from 2 to 402,
    # not 3).
    current_jobs=$(squeue -u "$USER" -h -r | wc -l)
    if [[ "$((current_jobs + chunk))" -gt "$SUBMIT_BUDGET" ]]; then
        remaining=$(( n - offset ))
        echo "search_backfill_vr.sh: stopping -- $current_jobs job(s) already queued, next shard ($chunk tasks) would exceed \$SUBMIT_BUDGET ($SUBMIT_BUDGET). $remaining run_id(s) still left; re-run this script once earlier shards have drained." >&2
        break
    fi

    job_id=$(sbatch --parsable -p "$PARTITION" -A "$ACCOUNT" -c "$CORES" --time "$TIME" --requeue \
        --array="1-${chunk}%${THROTTLE}" \
        --job-name "search-backfill-vr" \
        --output "$LOG_DIR/search_backfill_vr_%A_%a.out" --open-mode=append \
        --export="ALL,CHUNK_OFFSET=${offset}" \
        "$REPO_ROOT/results/search_backfill_vr.sh")
    echo "search_backfill_vr.sh: submitted array job $job_id (lines $((offset + 1))-$((offset + chunk)), throttle $THROTTLE)" >&2
    sed -n "$((offset + 1)),$((offset + chunk))p" "$RUN_ID_LIST" >> "$SUBMITTED_LIST"
    offset=$(( offset + chunk ))
done
