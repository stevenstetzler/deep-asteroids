#!/usr/bin/env bash
# Generates docs/results/: the full-survey funnel and summary plots, for #15.
#
# Unlike examples/night_summary.sh (one validation night, 60 searches), this
# covers the whole survey: ~64 nights, ~277k results, no --night/--field filter anywhere. $BAND
# (default VR) picks which searches the funnel and plots describe: the band='VR' searches
# (`search_backfill_vr.sh`, #28), or BAND=none for salad's 5,573 legacy (undefined-band) runs in
# $RESULTS_DB (salad's Postgres by default). Both are ingested/searched in the same database and every
# stage below takes --band, so the two never mix.
#
# What it does:
#   1. ingests every search definition and known tracklet (MPC observations,
#      mpchecker ephemerides, synthetic objects) deep-db knows about, and
#      links the MPC tracklets split across trkids;
#   2. ingests every run's results from $RESULTS_DB, de-duplicates them, and
#      cross-matches them against the known tracklets;
#   3. computes the per-result statistics and prints the funnel (`summary`);
#   4. writes the plots to $OUT_DIR (default docs/results/).
#
# Needs a $DEEP_ASTEROIDS_DB pointed at (or about to create) a database with
# room for the full survey -- a few GB, not the ~10GB home quota (see the
# README's own storage note) -- and $DEEP_DB reachable (fetch it once with
# `deep-asteroids database fetch-deep-db`, or set $DEEP_DB to an existing
# cached copy). Run from inside a Slurm job, not a login node: this ingests
# ~277k results and crossmatches them, real CPU work (took ~1h20m in total
# the first time, see docs/results/README.md).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

: "${RESULTS_DB:=postgresql://${USER}@localhost:55432/salad}"
: "${BAND:=VR}"
: "${OUT_DIR:=../docs/results}"
: "${DEEP_ASTEROIDS_DB:?Set DEEP_ASTEROIDS_DB, e.g. sqlite:////gscratch/<group>/$USER/deep_asteroids_full_survey.sqlite}"
: "${DEEP_DB:?Set DEEP_DB to a cached deep-db copy, e.g. from 'deep-asteroids database fetch-deep-db'}"
export DEEP_ASTEROIDS_DB DEEP_DB

deep-asteroids database create
deep-asteroids ingest searches
deep-asteroids ingest tracklets synthetic mpc-observations mpc-ephemeris
deep-asteroids link
deep-asteroids ingest results --results-db "$RESULTS_DB"  # the legacy (undefined-band) results; the VR ones come from `search run`
deep-asteroids dedup --band "$BAND"
deep-asteroids crossmatch --band "$BAND"
deep-asteroids statistics --band "$BAND"

echo; echo "== the funnel"
deep-asteroids summary --band "$BAND"

mkdir -p "$OUT_DIR"
deep-asteroids plot residuals --band "$BAND" --out "$OUT_DIR/residuals.png"
deep-asteroids plot sigma-v --band "$BAND" --sigma-v-threshold 0.00015 --out "$OUT_DIR/sigma_v.png"
deep-asteroids plot magnitudes --band "$BAND" --out "$OUT_DIR/magnitudes.png"
deep-asteroids plot completeness --band "$BAND" --out "$OUT_DIR/completeness.png" --source synthetic
