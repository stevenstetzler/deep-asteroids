#!/usr/bin/env bash
# The full-survey batch backfill (#32): renders every putative-real
# result's page (`deep-asteroids package result --putative-real`) at the
# sigma_v < 0.00015 cut, across the whole database -- unlike
# examples/package_backfill.sh (one `sbatch` job, used to demo/validate
# the --putative-real/--dry-run CLI flags against a single night), this
# submits one `sbatch` job PER NIGHT, each $CORES-wide within itself.
#
# Three modes, same script, told apart by $NIGHT/$INGEST_IMAGES:
#   - Submitter (neither set): a login-node-safe loop over `sbatch` and
#     `squeue` calls, no rendering itself. First submits ONE serial
#     ingest-images job (see below), then one render job per night with
#     at least one putative-real result (re-invoking this same script
#     with $NIGHT set), each depending on the ingest job
#     (`--dependency=afterok`), then one final `package index` job
#     depending on every render job (`--dependency=afterany`, so it still
#     runs and rewrites whatever did render even if some were preempted).
#     Safe to re-run: a night (or the ingest job) that already has a job
#     queued/running is detected via `squeue` and not resubmitted.
#   - Ingest job ($INGEST_IMAGES set): ingests every selected night's
#     images SERIALLY, one `deep-asteroids ingest images` call at a time
#     in a single process -- this *must* run alone, never concurrently
#     with itself across nights: the full-survey database is SQLite, a
#     single-writer store, and several `ingest images` calls writing at
#     once raises `sqlite3.OperationalError: database is locked` (hit
#     this for real the first time this script ran across many nights'
#     jobs in parallel -- see PR #33). ~15s/night empirically.
#   - Worker ($NIGHT set, inside that night's render job): fans `package
#     result --run-id <run_id> --putative-real --no-movie` for that night
#     out across $SLURM_CPUS_PER_TASK workers (`xargs -P`, never `nproc`).
#     `package_result()`/`visualize_result()` only read the database (see
#     #32's own note on this), so this part has no write-concurrency
#     issue -- the ingest-images step above is the only write, which is
#     why it's pulled out into its own serial, dependency-gated job.
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

nights_with_putative_real() {
    # All nights with >0 putative-real results, one per line. `|| true` on the inner query:
    # a night with zero matches makes `query results` exit 1 (ClickException), which
    # `pipefail` would otherwise propagate and, under `set -e`, kill the caller -- that's
    # expected here, not an error.
    local night n
    for night in $(deep-asteroids query searches night --header | tail -n +2 | sort -un); do
        n=$(deep-asteroids query results --night "$night" --putative-real \
            --sigma-v-threshold "$SIGMA_V_THRESHOLD" --header 2>/dev/null | tail -n +2 | wc -l || true)
        [[ "$n" -gt 0 ]] && echo "$night"
    done
}

if [[ -n "${INGEST_IMAGES:-}" ]]; then
    # Ingest job: ingests every selected night's images, one nights at a time, in this one
    # process -- see the header comment for why this must never run as more than one
    # concurrent writer against the (SQLite) full-survey database.
    [[ -n "${SLURM_JOB_ID:-}" ]] || {
        echo "package_backfill_putative_real.sh: \$INGEST_IMAGES is set but this isn't a Slurm job -- submit with sbatch, don't run this directly" >&2
        exit 1
    }
    echo "package_backfill_putative_real.sh: ingest job $SLURM_JOB_ID, restart count ${SLURM_RESTART_COUNT:-0}" >&2

    : "${REPO_DIR:=/gscratch/dirac/DEEP/repo}"
    : "${REGISTRY_HOST:=epyc.astro.washington.edu/deep_klone_2}"
    : "${PGUSER:?Set PGUSER, e.g. 'source /gscratch/dirac/DEEP/data/credentials' first}"
    : "${PGPASSWORD:?Set PGPASSWORD, e.g. 'source /gscratch/dirac/DEEP/data/credentials' first}"
    registry_uri="postgresql://${PGUSER}:${PGPASSWORD}@${REGISTRY_HOST}"

    for night in $(nights_with_putative_real); do
        echo "package_backfill_putative_real.sh: ingesting images for night $night" >&2
        deep-asteroids ingest images --registry-uri "$registry_uri" \
            --repo-dir "$REPO_DIR" --product-type differenceExp --night "$night"
    done
    echo "package_backfill_putative_real.sh: ingest job done" >&2
    exit 0
fi

if [[ -n "${NIGHT:-}" ]]; then
    # Worker: inside the per-night render job. Runs only after the ingest job above has
    # already committed every night's images (the submitter's --dependency=afterok ensures
    # this), so nothing here writes to the database -- package_result()/visualize_result()
    # only ever read it.
    [[ -n "${SLURM_JOB_ID:-}" ]] || {
        echo "package_backfill_putative_real.sh: \$NIGHT is set but this isn't a Slurm job -- submit with sbatch, don't run this directly" >&2
        exit 1
    }
    workers="${SLURM_CPUS_PER_TASK:-1}"
    echo "package_backfill_putative_real.sh: night $NIGHT: job $SLURM_JOB_ID, restart count ${SLURM_RESTART_COUNT:-0}" >&2

    # `|| true`: the submitter only sbatch's nights with >0 putative-real results, but guard
    # here too -- same pipefail/set -e trap as nights_with_putative_real()'s inner query.
    run_ids=$(deep-asteroids query results --night "$NIGHT" --putative-real \
        --sigma-v-threshold "$SIGMA_V_THRESHOLD" --header 2>/dev/null \
        | awk -F'|' 'NR>1{print $1}' | sort -u || true)
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

# Submitter: the ingest job, then one sbatch job per night (depending on it), then one final
# index job (depending on all of those).
mkdir -p "$LOG_DIR"

existing_ingest=$(squeue -u "$USER" -n "package-backfill-ingest" -h -o "%A" 2>/dev/null || true)
if [[ -n "$existing_ingest" ]]; then
    echo "package_backfill_putative_real.sh: ingest job already submitted ($existing_ingest), skipping" >&2
    ingest_job_id="$existing_ingest"
else
    ingest_job_id=$(sbatch --parsable -p "$PARTITION" -A "$ACCOUNT" -c 1 --time 02:00:00 --requeue \
        --job-name "package-backfill-ingest" \
        --output "$LOG_DIR/package_backfill_ingest_%j.out" --open-mode=append \
        --export=ALL,INGEST_IMAGES=1 \
        "$SCRIPT_PATH")
    echo "package_backfill_putative_real.sh: submitted ingest job $ingest_job_id" >&2
fi

job_ids=()
for night in $(nights_with_putative_real); do
    # Idempotent resubmission: if the submitter itself died partway through (or is just being
    # re-run), don't double-submit a night that already has a job -- reuse its ID for the final
    # index job's dependency instead.
    existing=$(squeue -u "$USER" -n "package-backfill-$night" -h -o "%A" 2>/dev/null || true)
    if [[ -n "$existing" ]]; then
        echo "package_backfill_putative_real.sh: night $night: already submitted ($existing), skipping" >&2
        job_ids+=($existing)
        continue
    fi

    job_id=$(sbatch --parsable -p "$PARTITION" -A "$ACCOUNT" -c "$CORES" --time "$TIME" --requeue \
        --dependency "afterok:$ingest_job_id" \
        --job-name "package-backfill-$night" \
        --output "$LOG_DIR/package_backfill_${night}_%j.out" --open-mode=append \
        --export=ALL,NIGHT="$night" \
        "$SCRIPT_PATH")
    echo "package_backfill_putative_real.sh: night $night: submitted job $job_id (depends on ingest job $ingest_job_id)" >&2
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
