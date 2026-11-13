#!/usr/bin/env bash
# Generates docs/results/: the full-survey funnel and summary plots, for #15.
#
# Unlike examples/night_summary.sh (one validation night, 60 searches), this
# covers the whole survey: all 5,573 runs in $RESULTS_DB (salad's Postgres by
# default), ~64 nights, ~277k results. No --night/--field filter anywhere.
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
: "${OUT_DIR:=../docs/results}"
: "${DEEP_ASTEROIDS_DB:?Set DEEP_ASTEROIDS_DB, e.g. sqlite:////gscratch/<group>/$USER/deep_asteroids_full_survey.sqlite}"
: "${DEEP_DB:?Set DEEP_DB to a cached deep-db copy, e.g. from 'deep-asteroids database fetch-deep-db'}"
export DEEP_ASTEROIDS_DB DEEP_DB

deep-asteroids database create
deep-asteroids ingest searches
deep-asteroids ingest tracklets synthetic mpc-observations mpc-ephemeris
deep-asteroids link
deep-asteroids ingest results --results-db "$RESULTS_DB"
deep-asteroids dedup
deep-asteroids crossmatch
deep-asteroids statistics

echo; echo "== the funnel"
deep-asteroids summary

mkdir -p "$OUT_DIR"
deep-asteroids plot residuals --out "$OUT_DIR/residuals.png"
deep-asteroids plot sigma-v --out "$OUT_DIR/sigma_v.png"
deep-asteroids plot magnitudes --out "$OUT_DIR/magnitudes.png"
deep-asteroids plot completeness --out "$OUT_DIR/completeness.png" --source synthetic
