#!/usr/bin/env bash
# The full-survey batch backfill (#32): renders every putative-real
# result's page (`deep-asteroids package result --putative-real`) at the
# sigma_v < 0.00015 cut, across the whole database -- unlike
# examples/package_backfill.sh (one `sbatch` job, used to demo/validate
# the --putative-real/--dry-run CLI flags against a single night), this
# submits one `sbatch` job PER NIGHT, each $CORES-wide within itself.
#
# Two modes, same script, told apart by $NIGHT:
#   - Submitter ($NIGHT unset): a login-node-safe loop that finds every
#     night with at least one putative-real result and `sbatch`'s one job
#     per night (re-invoking this same script with $NIGHT set) -- does no
#     rendering itself, just enumeration and `sbatch` calls, plus one
#     final `package index` job that depends on every night job finishing
#     (`--dependency=afterany`, so it still runs and rewrites whatever did
#     render even if some night jobs were preempted).
#   - Worker ($NIGHT set, inside that night's Slurm job): ingests that
#     night's images first (idempotent -- needed since `package result`
#     reads cutouts from `image` rows that full_survey_summary.sh never
#     ingests; ~15s/night empirically, see PR #33), then fans `package
#     result --run-id <run_id> --putative-real --no-movie` for that night
#     out across $SLURM_CPUS_PER_TASK workers (`xargs -P`, never `nproc`).
#     --no-movie skips movie.gif, by far the largest artifact (see PR #33's
#     storage estimate) -- the page and PDF report are unaffected either
#     way, so this is a pure storage cut.
#
# Usage (from a login node -- the submitter only calls `sbatch`):
#
#   DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/.../deep_asteroids_full_survey.sqlite \
#   DEEP_DB=/path/to/cached/deep_database.sqlite \
#   results/package_backfill_putative_real.sh
#
# $SIGMA_V_THRESHOLD (default 0.00015), $CORES (default 16), $PARTITION
# (default ckpt-all), $ACCOUNT (default ckpt-astro -- the real Hyak
# account name is ckpt-<group>, not <group>-ckpt; check with
# `sacctmgr show assoc where user=$USER` if unsure), $TIME (default
# 6:00:00, per-job wall time; the busiest night rendered in our estimate
# is ~4.2h at 16 cores -- see PR #33), $WEB_DIR (default ../web) and
# $LOG_DIR (default ./logs) all override.
#
# ckpt-all/ckpt-astro is preemptible -- a preempted job is automatically
# requeued by Slurm (PreemptMode=REQUEUE on this partition; `--requeue` is
# also passed explicitly at submission, as a second guarantee) and simply
# restarts this same script from the top under the same job ID (`%j` in
# $LOG_DIR, hence --open-mode=append so a requeue doesn't wipe the
# previous attempt's log -- each attempt's first line names its restart
# count, $SLURM_RESTART_COUNT). That restart is always safe to just redo:
# `ingest images` is idempotent per (night, field) group, and
# `package_result()`'s own skip-if-index.html-exists check is the whole
# manifest for everything after it (#32's "no new tracking table"), so
# nothing here tracks progress separately -- a requeue only (re)does
# whatever wasn't finished. That check is sound against a mid-write kill,
# too: render_result_page() (web/site.py) writes index.html via a
# same-directory temp file + atomic rename, so a worker killed mid-render
# never leaves a truncated index.html that a later run would mistake for
# "done".
set -euo pipefail
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "$SCRIPT_PATH")/.."

: "${DEEP_ASTEROIDS_DB:?Set DEEP_ASTEROIDS_DB to the full-survey database}"
: "${DEEP_DB:?Set DEEP_DB to a cached deep-db copy}"
: "${SIGMA_V_THRESHOLD:=0.00015}"
: "${CORES:=16}"
: "${PARTITION:=ckpt-all}"
: "${ACCOUNT:=ckpt-astro}"
: "${TIME:=6:00:00}"
: "${WEB_DIR:=$(pwd)/web}"
: "${LOG_DIR:=$(pwd)/results/logs}"
export DEEP_ASTEROIDS_DB DEEP_DB SIGMA_V_THRESHOLD CORES WEB_DIR

if [[ -n "${NIGHT:-}" ]]; then
    # Worker: inside the per-night Slurm job.
    [[ -n "${SLURM_JOB_ID:-}" ]] || {
        echo "package_backfill_putative_real.sh: \$NIGHT is set but this isn't a Slurm job -- submit with sbatch, don't run this directly" >&2
        exit 1
    }
    workers="${SLURM_CPUS_PER_TASK:-1}"
    echo "package_backfill_putative_real.sh: night $NIGHT: job $SLURM_JOB_ID, restart count ${SLURM_RESTART_COUNT:-0}" >&2

    : "${REPO_DIR:=/gscratch/dirac/DEEP/repo}"
    : "${REGISTRY_HOST:=epyc.astro.washington.edu/deep_klone_2}"
    : "${PGUSER:?Set PGUSER, e.g. 'source /gscratch/dirac/DEEP/data/credentials' first}"
    : "${PGPASSWORD:?Set PGPASSWORD, e.g. 'source /gscratch/dirac/DEEP/data/credentials' first}"
    deep-asteroids ingest images \
        --registry-uri "postgresql://${PGUSER}:${PGPASSWORD}@${REGISTRY_HOST}" \
        --repo-dir "$REPO_DIR" --product-type differenceExp --night "$NIGHT"

    run_ids=$(deep-asteroids query results --night "$NIGHT" --putative-real \
        --sigma-v-threshold "$SIGMA_V_THRESHOLD" --header | awk -F'|' 'NR>1{print $1}' | sort -u)
    n_run_ids=$(grep -c . <<< "$run_ids" || true)
    echo "package_backfill_putative_real.sh: night $NIGHT: $n_run_ids run_id(s), $workers worker(s) (\$SLURM_CPUS_PER_TASK)" >&2

    if [[ "$n_run_ids" -gt 0 ]]; then
        xargs -P "$workers" -I{} \
            deep-asteroids package result --run-id {} --putative-real --no-movie \
                --sigma-v-threshold "$SIGMA_V_THRESHOLD" --web-dir "$WEB_DIR" <<< "$run_ids"
    fi
    echo "package_backfill_putative_real.sh: night $NIGHT done" >&2
    exit 0
fi

# Submitter: one sbatch job per night with at least one putative-real result.
mkdir -p "$LOG_DIR"
nights=$(deep-asteroids query searches night --header | tail -n +2 | sort -un)

job_ids=()
for night in $nights; do
    n=$(deep-asteroids query results --night "$night" --putative-real \
        --sigma-v-threshold "$SIGMA_V_THRESHOLD" --header 2>/dev/null | tail -n +2 | wc -l)
    if [[ "$n" -eq 0 ]]; then
        continue
    fi
    job_id=$(sbatch --parsable -p "$PARTITION" -A "$ACCOUNT" -c "$CORES" --time "$TIME" --requeue \
        --job-name "package-backfill-$night" \
        --output "$LOG_DIR/package_backfill_${night}_%j.out" --open-mode=append \
        --export=ALL,NIGHT="$night" \
        "$SCRIPT_PATH")
    echo "package_backfill_putative_real.sh: night $night: $n putative-real result(s), submitted job $job_id" >&2
    job_ids+=("$job_id")
done

if [[ "${#job_ids[@]}" -eq 0 ]]; then
    echo "package_backfill_putative_real.sh: no putative-real results selected (run \`statistics\`/\`crossmatch\`/\`dedup\` first?)" >&2
    exit 1
fi

dependency=$(IFS=:; echo "afterany:${job_ids[*]}")
index_job_id=$(sbatch --parsable -p "$PARTITION" -A "$ACCOUNT" -c 1 --time 00:30:00 \
    --dependency "$dependency" \
    --job-name "package-backfill-index" \
    --output "$LOG_DIR/package_backfill_index_%j.out" --open-mode=append \
    --wrap "deep-asteroids package index --web-dir '$WEB_DIR'")
echo "package_backfill_putative_real.sh: ${#job_ids[@]} night job(s) submitted, index job $index_job_id depends on all of them" >&2
