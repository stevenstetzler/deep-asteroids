#!/usr/bin/env bash
# Runs one night's find-asteroids searches as a Slurm job ARRAY (one task per run_id), for when a
# sequential run (night_summary.sh, or the per-definition CLI loop it drives) would take too long --
# e.g. a deeper search (#43: a lower --threshold and/or finer --dx) over a whole night's ~180
# searches. Modeled on results/search_backfill_vr.sh's full-survey array (see that script for the
# full design rationale: one array task per run_id, not a per-night fan-out, because `search run`
# is written to own its whole job's CPU budget per run_id); this is the single-night-scoped version,
# small enough it never needs that script's sharding/submission-budget machinery.
#
# $DEEP_ASTEROIDS_DB must be Postgres, not SQLite: array tasks can land on different compute nodes,
# and SQLite's WAL concurrency (deep_asteroids.database.get_db()'s own docstring) needs every writer
# on one host -- exactly what #47 moved the production database off SQLite to avoid. Point it at a
# fresh database (`createdb`) for a deeper-search variant, so it doesn't overwrite the default
# threshold/dx results for the same run_ids (`search run` replaces a run_id's results in place).
#
# Use the Postgres server's real node hostname (e.g. n3064.hyak.local, not plain "localhost"/
# 127.0.0.1): an array task on a *different* node than the server has no "localhost" route to it --
# confirmed the hard way, a submission against "localhost" ran fine on the submitter's own node but
# every task Slurm placed elsewhere failed with "connection refused". `getent hosts $(hostname -s)`
# on the node actually running `postgres -D ...` gives its routable name.
#
# Needs the night's search definitions, tracklets and images already ingested -- run
# night_summary.sh's own first few steps (ingest searches/tracklets/link, then ingest images) first,
# or do it directly; this script only runs `search run`, as an array instead of a loop.
#
# Usage (from a login node -- the submitter only lists run_ids and calls `sbatch`; array tasks are
# the only thing that actually searches):
#
#   export LD_LIBRARY_PATH="$(pg_config --libdir):$LD_LIBRARY_PATH"   # psycopg2 needs libpq.so.5
#   DEEP_ASTEROIDS_DB=postgresql://$USER@n3064.hyak.local:55432/deep_asteroids_snr_3 \
#   DEEP_DB=/path/to/cached/deep_database.sqlite \
#   NIGHT=20190402 THRESHOLD=3 DX=10 \
#   ./search_night_array.sh
#
# $BAND (default VR), $CORES (default 4), $THROTTLE (default 32 -- check idle cores first, e.g.
# `sinfo -p $PARTITION -N -o "%N %C"`), $PARTITION (default compute-bigmem), $ACCOUNT (default
# compute-bigmem-astro), $TIME (per-task wall time, default 00:15:00), $CATALOG_DIR (default
# /gscratch/scrubbed/$USER/deep_asteroids/catalogs -- shared across threshold/dx variants is fine,
# catalogs are keyed by exposure id only, not by search parameters), $EXCLUDE_MASK_PLANES
# (comma-separated, default: the engine's own default list -- a deeper/noisier search can widen it,
# e.g. add CR,INTRP for a bad column the default doesn't cover; see night_summary.sh's own note),
# $NUM_RESULTS (find-asteroids' own ranked-results-per-search cap, default 50) all override. Re-running
# the submitter after a partial array (or once it's done) only submits what's left
# (list_night_run_ids.py excludes any run_id that already has a `search` row). $FORCE=1 instead
# re-searches *every* run_id of the night/band, including ones already searched (`--force`'s own
# in-place replace) -- use this to redo an already-completed night with different parameters, e.g. a
# higher $NUM_RESULTS, without starting from a fresh database.
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_ROOT"

: "${DEEP_ASTEROIDS_DB:?Set DEEP_ASTEROIDS_DB to a postgresql:// URL (see the header comment above)}"
: "${DEEP_DB:?Set DEEP_DB to a cached deep-db copy}"
: "${NIGHT:?Set NIGHT}"
: "${BAND:=VR}"
: "${THRESHOLD:=5.0}"
: "${DX:=10.0}"
: "${CORES:=4}"
: "${THROTTLE:=32}"
: "${PARTITION:=compute-bigmem}"
: "${ACCOUNT:=compute-bigmem-astro}"
: "${TIME:=00:15:00}"
: "${CATALOG_DIR:=/gscratch/scrubbed/$USER/deep_asteroids/catalogs}"
: "${EXCLUDE_MASK_PLANES:=}"  # default: unset -- the engine's own default plane list applies
: "${NUM_RESULTS:=50}"  # find-asteroids' own default; raise for a deeper ranked-results list (e.g. #43 follow-up)
: "${FORCE:=0}"  # 1: re-search run_ids that already have a `search` row (e.g. a NUM_RESULTS/THRESHOLD/DX
                 # change for an already-searched night), passing `--force` and listing them too
: "${LOG_DIR:=$REPO_ROOT/examples/logs}"
: "${RUN_ID_LIST:=$REPO_ROOT/examples/search_night_${NIGHT}_run_ids.txt}"
case "$DEEP_ASTEROIDS_DB" in
    sqlite*) echo "search_night_array.sh: \$DEEP_ASTEROIDS_DB must be Postgres, not SQLite -- array tasks can land on different nodes (see this script's own docstring)" >&2; exit 2 ;;
esac
export DEEP_ASTEROIDS_DB DEEP_DB REPO_ROOT CATALOG_DIR THRESHOLD DX EXCLUDE_MASK_PLANES NUM_RESULTS FORCE RUN_ID_LIST

if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
    # Worker: one array task, one run_id -- line $SLURM_ARRAY_TASK_ID of $RUN_ID_LIST.
    run_id=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$RUN_ID_LIST")
    [[ -n "$run_id" ]] || {
        echo "search_night_array.sh: no run_id at line $SLURM_ARRAY_TASK_ID of $RUN_ID_LIST" >&2
        exit 1
    }
    echo "search_night_array.sh: task $SLURM_ARRAY_TASK_ID: $run_id, job $SLURM_JOB_ID" >&2
    MASK_OPT=()
    [[ -n "$EXCLUDE_MASK_PLANES" ]] && MASK_OPT=(--exclude-mask-planes "$EXCLUDE_MASK_PLANES")
    FORCE_OPT=()
    [[ "$FORCE" == 1 ]] && FORCE_OPT=(--force)
    deep-asteroids search run --run-id "$run_id" --catalog-dir "$CATALOG_DIR" --threshold "$THRESHOLD" --dx "$DX" \
        --num-results "$NUM_RESULTS" "${MASK_OPT[@]}" "${FORCE_OPT[@]}"
    echo "search_night_array.sh: task $SLURM_ARRAY_TASK_ID: $run_id done" >&2
    exit 0
fi

# Submitter: every (night, band) run_id with enough image coverage that doesn't already have a
# `search` row (list_night_run_ids.py) -- so a re-run after a partial array only submits the rest.
# $FORCE=1 instead lists (and re-searches) every one, including those already searched.
mkdir -p "$LOG_DIR"
INCLUDE_SEARCHED_OPT=()
[[ "$FORCE" == 1 ]] && INCLUDE_SEARCHED_OPT=(--include-searched)
python3 examples/list_night_run_ids.py --night "$NIGHT" --band "$BAND" "${INCLUDE_SEARCHED_OPT[@]}" > "$RUN_ID_LIST"
n=$(wc -l < "$RUN_ID_LIST")
if [[ "$n" -eq 0 ]]; then
    echo "search_night_array.sh: nothing left to search for night $NIGHT band $BAND (already done, or none has enough image coverage)" >&2
    exit 0
fi

job_id=$(sbatch --parsable -p "$PARTITION" -A "$ACCOUNT" -c "$CORES" --time "$TIME" \
    --array="1-${n}%${THROTTLE}" \
    --job-name "search-night-${NIGHT}" \
    --output "$LOG_DIR/search_night_${NIGHT}_%A_%a.out" \
    "$REPO_ROOT/examples/search_night_array.sh")
echo "search_night_array.sh: submitted array job $job_id, $n run_id(s) from $RUN_ID_LIST, throttle $THROTTLE" >&2
echo "search_night_array.sh: watch with: squeue -u \$USER -j $job_id" >&2
