#!/usr/bin/env bash
# Merges this run's already-rendered per-result PDFs (package_backfill_putative_real.sh) into
# one PDF per (night, group) -- #32/#33's mpchecker/synthetic/putative-new groups, the ones
# docs/results/magnitudes.png highlights. See README.md's "Merged PDFs by group" section for
# what each group means.
#
# Unlike the backfill itself, this is cheap (no subprocess engine, just concatenating small
# already-rendered PDFs) -- it runs fine on a login node, no Slurm job needed. Extra arguments
# (e.g. --night, --group, --force) pass straight through to `package merge-pdfs`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${DEEP_ASTEROIDS_DB:?Set DEEP_ASTEROIDS_DB to the full-survey database}"
: "${WEB_DIR:=$(pwd)/web}"
: "${SIGMA_V_THRESHOLD:=0.00015}"
: "${BAND:=VR}"  # which band's rendered PDFs to merge; none = the undefined-band legacy ones
export DEEP_ASTEROIDS_DB

deep-asteroids package merge-pdfs --web-dir "$WEB_DIR" --sigma-v-threshold "$SIGMA_V_THRESHOLD" --band "$BAND" "$@"
