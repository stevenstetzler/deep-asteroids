#!/usr/bin/env bash
# Summarizes a night's find-asteroids results (the statistics, putative-real filter and plots):
# per-result statistics, the funnel of what the results match and which are putative real, and plots of the
# residuals, magnitudes and the synthetic-object completeness. The night's results are salad's (copied from
# $RESULTS_DB, default salad's Postgres on localhost:55432 -- start it first), all 60 searches of 20190402.
#
# What it does:
#   1. ingests the night's search definitions and known tracklets, links the MPC tracklets;
#   2. ingests the night's results, de-duplicates them (so objects are counted once, across CCDs and
#      searches) and cross-matches them against the known tracklets;
#   3. computes the per-result statistics, prints the funnel (`summary`), and lists the putative-new candidates;
#   4. writes the plots to $OUT_DIR (default docs/examples/viz/night_<NIGHT>).
#
# NIGHT=20190403 ./night_summary.sh summarizes another night salad searched.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# common.sh asks for the Butler registry credentials; this example never reads the registry
export PGUSER="${PGUSER:-unused}" PGPASSWORD="${PGPASSWORD:-unused}"
source ./common.sh

: "${NIGHT:=20190402}"
: "${RESULTS_DB:=postgresql://${USER}@localhost:55432/salad}"
: "${OUT_DIR:=../docs/examples/viz/night_${NIGHT}}"

deep-asteroids ingest searches --night "$NIGHT"
deep-asteroids ingest tracklets synthetic mpc-observations mpc-ephemeris --night "$NIGHT"
deep-asteroids link --night "$NIGHT"
deep-asteroids ingest results --results-db "$RESULTS_DB" --night "$NIGHT"
deep-asteroids dedup --night "$NIGHT"
deep-asteroids crossmatch --night "$NIGHT"
deep-asteroids statistics --night "$NIGHT"

echo; echo "== the funnel"
deep-asteroids summary --night "$NIGHT"

echo; echo "== the first putative-new candidates (real-looking, matching no known object)"
deep-asteroids query results --night "$NIGHT" --putative-new --authoritative-only --limit 5 --header | column -s'|' -t

deep-asteroids plot residuals --night "$NIGHT" --out "$OUT_DIR/residuals_by_group.png"
deep-asteroids plot magnitudes --night "$NIGHT" --out "$OUT_DIR/magnitudes_by_group.png"
deep-asteroids plot completeness --night "$NIGHT" --out "$OUT_DIR/completeness_synthetic.png"
deep-asteroids plot completeness --night "$NIGHT" --source mpchecker --out "$OUT_DIR/completeness_known.png"
