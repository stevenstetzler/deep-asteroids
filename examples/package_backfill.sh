#!/usr/bin/env bash
# The batch backfill proposed in #32: render every putative-real result's
# page (`deep-asteroids package result`) across the whole database. Unlike
# the rest of examples/, which ingest and search a small toy case from
# scratch, this one assumes `ingest searches`/`ingest images`/`search
# run`/`statistics`/`crossmatch`/`dedup` are already done against
# $DEEP_ASTEROIDS_DB (the real database -- override it the same way the
# main README does, not the throwaway one the other examples build) and
# just fills in web/ pages from what's already there.
#
# Parallelizes across run_ids (search definitions), never within one --
# `package_result()`/`visualize_result()` only read the database and write
# to their own result directory, so any number of `package result` calls
# for different run_ids run concurrently with zero contention (see #32).
# Sized from the Slurm allocation ($SLURM_CPUS_PER_TASK), never nproc/the
# whole node -- run this from an `sbatch` job, e.g.:
#
#   sbatch --partition=<partition> --account=<account> -c 16 --mem=32G \
#       --time=8:00:00 --wrap='examples/package_backfill.sh'
#
# A worker, or the whole job, that dies partway through is safe to just
# re-run: `package_result()` already skips a result whose index.html
# exists -- the filesystem is the manifest, not a new queue table (#32's
# "no new tracking table") -- so a resumed/resubmitted run only (re)does
# the gap. Set $NIGHT to narrow the batch to one night instead of the
# whole database.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${WEB_DIR:=web}"
: "${NIGHT:=}"
night_arg=()
[[ -n "$NIGHT" ]] && night_arg=(--night "$NIGHT")

if [[ -z "${SLURM_JOB_ID:-}" ]]; then
    echo "package_backfill.sh: run this from a Slurm job (salloc/sbatch), not a login node" >&2
    exit 1
fi
workers="${SLURM_CPUS_PER_TASK:-1}"

echo "package_backfill.sh: enumerating run_ids with putative-real results..." >&2
run_ids=$(deep-asteroids query results "${night_arg[@]}" --putative-real --header \
    | awk -F'|' 'NR>1{print $1}' | sort -u)
n_run_ids=$(grep -c . <<< "$run_ids" || true)

if [[ "$n_run_ids" -eq 0 ]]; then
    echo "package_backfill.sh: no putative-real results selected (run \`statistics\`/\`crossmatch\`/\`dedup\` first?)" >&2
    exit 1
fi
echo "package_backfill.sh: $n_run_ids run_id(s), $workers worker(s) (\$SLURM_CPUS_PER_TASK)" >&2

xargs -P "$workers" -I{} \
    deep-asteroids package result --run-id {} --putative-real --web-dir "$WEB_DIR" <<< "$run_ids"

deep-asteroids package index --web-dir "$WEB_DIR"
echo "package_backfill.sh: done" >&2
