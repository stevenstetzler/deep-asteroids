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
# 00:10:00 -- comfortably above the ~77s slowest calibration run), $CATALOG_DIR (default
# /gscratch/scrubbed/$USER/deep_asteroids/catalogs -- scrubbed, so don't let this run span
# the ~21-day inactivity window) all override.
#
# ckpt-all/ckpt-astro is preemptible -- `--requeue` is passed explicitly (on top of the
# partition's own PreemptMode=REQUEUE), `--open-mode=append` keeps a requeued task's log
# instead of wiping it, and each task logs its own restart count. `run_search_for_definition`
# (#37) already skips a run_id with an existing `search` row unless --force, and now commits
# its results and its own SearchRun provenance row atomically in one transaction -- so a
# requeued or resubmitted task is always either fully done or not started at all, never
# missing just its SearchRun row the way an interrupted run used to be able to.
set -euo pipefail
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "$SCRIPT_PATH")/.."

: "${DEEP_ASTEROIDS_DB:?Set DEEP_ASTEROIDS_DB to the full-survey database}"
: "${DEEP_DB:?Set DEEP_DB to a cached deep-db copy}"
: "${CORES:=4}"
: "${THROTTLE:=16}"
: "${PARTITION:=ckpt-all}"
: "${ACCOUNT:=ckpt-astro}"
: "${TIME:=00:10:00}"
: "${CATALOG_DIR:=/gscratch/scrubbed/$USER/deep_asteroids/catalogs}"
: "${LOG_DIR:=$(pwd)/results/logs}"
: "${RUN_ID_LIST:=$(pwd)/results/vr_run_ids.txt}"
export DEEP_ASTEROIDS_DB DEEP_DB

if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
    # Worker: one array task, one run_id (line $SLURM_ARRAY_TASK_ID of $RUN_ID_LIST).
    run_id=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$RUN_ID_LIST")
    [[ -n "$run_id" ]] || {
        echo "search_backfill_vr.sh: no run_id at line $SLURM_ARRAY_TASK_ID of $RUN_ID_LIST" >&2
        exit 1
    }
    echo "search_backfill_vr.sh: task $SLURM_ARRAY_TASK_ID: $run_id, job $SLURM_JOB_ID, restart count ${SLURM_RESTART_COUNT:-0}" >&2

    deep-asteroids search run --run-id "$run_id" --catalog-dir "$CATALOG_DIR" --remove-catalogs
    echo "search_backfill_vr.sh: task $SLURM_ARRAY_TASK_ID: $run_id done" >&2
    exit 0
fi

# Submitter: materialize the run_id list (idempotent -- always recomputed fresh from the
# database's current state), then submit one array job over it.
mkdir -p "$LOG_DIR" "$(dirname "$RUN_ID_LIST")"
python3 results/list_vr_run_ids.py > "$RUN_ID_LIST"
n=$(wc -l < "$RUN_ID_LIST")
if [[ "$n" -eq 0 ]]; then
    echo "search_backfill_vr.sh: no in-scope, image-complete VR run_ids found" >&2
    exit 1
fi
echo "search_backfill_vr.sh: $n run_id(s) in $RUN_ID_LIST, submitting array (throttle %$THROTTLE)" >&2

job_id=$(sbatch --parsable -p "$PARTITION" -A "$ACCOUNT" -c "$CORES" --time "$TIME" --requeue \
    --array="1-${n}%${THROTTLE}" \
    --job-name "search-backfill-vr" \
    --output "$LOG_DIR/search_backfill_vr_%A_%a.out" --open-mode=append \
    "$SCRIPT_PATH")
echo "search_backfill_vr.sh: submitted array job $job_id ($n tasks, throttle $THROTTLE)" >&2
