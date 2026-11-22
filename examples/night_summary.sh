#!/usr/bin/env bash
# Summarizes a night's find-asteroids results (the statistics, putative-real filter and plots):
# per-result statistics, the funnel of what the results match and which are putative real, and plots of the
# residuals, magnitudes and the synthetic-object completeness. By default ($BAND=VR) the night's results are
# those of the band='VR' searches, run here from the Butler images (`search run`); BAND=none summarizes salad's
# legacy (undefined-band) searches instead, copied from $RESULTS_DB (default salad's Postgres on
# localhost:55432 -- start it first). All 60 searches of 20190402 either way.
#
# What it does:
#   1. ingests the night's search definitions and known tracklets, links the MPC tracklets;
#   2. runs the night's searches (BAND=VR; needs `ingest images`) or ingests salad's results (BAND=none),
#      de-duplicates the band's results (so objects are counted once, across CCDs and searches) and
#      cross-matches them against the known tracklets;
#   3. computes the per-result statistics, prints the funnel (`summary`), and lists the putative-new candidates;
#   4. writes the plots to $OUT_DIR (default docs/examples/viz/night_<NIGHT>).
#
# NIGHT=20190403 ./night_summary.sh summarizes another night.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# common.sh asks for the Butler registry credentials; this example never reads the registry
export PGUSER="${PGUSER:-unused}" PGPASSWORD="${PGPASSWORD:-unused}"
source ./common.sh

: "${NIGHT:=20190402}"
: "${BAND:=VR}"
: "${RESULTS_DB:=postgresql://${USER}@localhost:55432/salad}"
: "${OUT_DIR:=../docs/examples/viz/night_${NIGHT}}"

deep-asteroids ingest searches --night "$NIGHT"
deep-asteroids ingest tracklets synthetic mpc-observations mpc-ephemeris --night "$NIGHT"
deep-asteroids link --night "$NIGHT"
if [[ "$BAND" == none ]]; then
    deep-asteroids ingest results --results-db "$RESULTS_DB" --night "$NIGHT"
else
    deep-asteroids search run --night "$NIGHT" --band "$BAND"
fi
deep-asteroids dedup --night "$NIGHT" --band "$BAND"
deep-asteroids crossmatch --night "$NIGHT" --band "$BAND"
deep-asteroids statistics --night "$NIGHT" --band "$BAND"

echo; echo "== the funnel"
deep-asteroids summary --night "$NIGHT" --band "$BAND"

echo; echo "== the first putative-new candidates (real-looking, matching no known object)"
deep-asteroids query results --night "$NIGHT" --putative-new --authoritative-only --band "$BAND" --limit 5 --header | column -s'|' -t

deep-asteroids plot residuals --night "$NIGHT" --band "$BAND" --out "$OUT_DIR/residuals_by_group.png"
deep-asteroids plot sigma-v --night "$NIGHT" --band "$BAND" --out "$OUT_DIR/sigma_v_by_group.png"
deep-asteroids plot magnitudes --night "$NIGHT" --band "$BAND" --out "$OUT_DIR/magnitudes_by_group.png"
deep-asteroids plot completeness --night "$NIGHT" --band "$BAND" --out "$OUT_DIR/completeness_synthetic.png"
deep-asteroids plot completeness --night "$NIGHT" --band "$BAND" --source mpchecker --out "$OUT_DIR/completeness_known.png"
