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
#   2. runs the night's searches (BAND=VR; needs `ingest images` -- see $INGEST_IMAGES) or ingests salad's
#      results (BAND=none), de-duplicates the band's results (so objects are counted once, across CCDs and
#      searches) and cross-matches them against the known tracklets;
#   3. computes the per-result statistics, prints the funnel (`summary`), and lists the putative-new candidates;
#   4. writes the plots to $OUT_DIR (default docs/examples/viz/night_<NIGHT>).
#
# NIGHT=20190403 ./night_summary.sh summarizes another night.
#
# A deeper search (#43): $THRESHOLD (detection sigma, default 5.0) and $DX (default 10.0) are passed
# straight to `search run`; $CATALOG_DIR likewise (default: deep-asteroids' own
# /gscratch/scrubbed/$USER/deep_asteroids/catalogs), set it to a different directory for a non-default
# threshold/dx so its catalogs don't mix with another run's. Point $DEEP_ASTEROIDS_DB at a fresh,
# separate database for a deeper-search variant -- `search run` replaces a run_id's results in place
# (`--force`), so a second threshold/dx for the same night in the *same* database would overwrite the
# first rather than keep both side by side. A fresh database has no images yet either: set
# $INGEST_IMAGES=1 (needs the real Butler registry credentials -- `source
# /gscratch/dirac/DEEP/data/credentials` -- unlike the default run against an already-populated one):
#
#   DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/$USER/deep_asteroids_snr_3.sqlite \
#   CATALOG_DIR=/gscratch/scrubbed/$USER/deep_asteroids/catalogs_snr_3 \
#   OUT_DIR=../docs/examples/viz/night_20190402_snr_3 \
#   THRESHOLD=3 INGEST_IMAGES=1 \
#   ./night_summary.sh
#
# A deeper (lower-threshold) search turns up artifacts the engine's default --exclude-mask-planes
# doesn't cover: a CR-interpolated bad column, for instance, showed up as hundreds of real-looking
# detections in one ccd41 search at threshold=3 (#43) -- its footprints carry CR/INTRP, but neither
# plane is in the engine's default list (matched to the reference pipeline's own default; see
# util/detect.py's DEFAULT_EXCLUDE_MASK_PLANES). $EXCLUDE_MASK_PLANES (comma-separated, e.g.
# "EDGE,SAT,SUSPECT,BAD,NO_DATA,STREAK,CROSSTALK,NOT_DEBLENDED,CR,INTRP") widens it for this run only.
#
# A whole night's ~180 searches run one at a time here, which can take hours. For a faster, parallel
# run across a Slurm partition, do the ingest/images steps as above (DEEP_ASTEROIDS_DB=... INGEST_IMAGES=1
# ./night_summary.sh, then Ctrl-C once `search run` starts, or set up to that point by hand) and use
# `search_night_array.sh` instead of the `search run` step -- a job ARRAY, one task per run_id (see
# that script's own docstring). It needs `$DEEP_ASTEROIDS_DB` to be Postgres, not SQLite (array tasks
# can land on different compute nodes; SQLite's WAL concurrency needs one host -- #47).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
: "${INGEST_IMAGES:=0}"
if [[ "$INGEST_IMAGES" != 1 ]]; then
    # common.sh asks for the Butler registry credentials; this example doesn't read the registry
    # unless it has to ingest images itself (a fresh database with none yet -- see $INGEST_IMAGES above)
    export PGUSER="${PGUSER:-unused}" PGPASSWORD="${PGPASSWORD:-unused}"
fi
source ./common.sh

: "${NIGHT:=20190402}"
: "${BAND:=VR}"
: "${RESULTS_DB:=postgresql://${USER}@localhost:55432/salad}"
: "${OUT_DIR:=../docs/examples/viz/night_${NIGHT}}"
: "${THRESHOLD:=5.0}"
: "${DX:=10.0}"
: "${CATALOG_DIR:=}"
: "${EXCLUDE_MASK_PLANES:=}"  # default: unset -- the engine's own default plane list applies
CATALOG_OPT=()
[[ -n "$CATALOG_DIR" ]] && CATALOG_OPT=(--catalog-dir "$CATALOG_DIR")
MASK_OPT=()
[[ -n "$EXCLUDE_MASK_PLANES" ]] && MASK_OPT=(--exclude-mask-planes "$EXCLUDE_MASK_PLANES")

deep-asteroids ingest searches --night "$NIGHT"
deep-asteroids ingest tracklets synthetic mpc-observations mpc-ephemeris --night "$NIGHT"
deep-asteroids link --night "$NIGHT"
if [[ "$INGEST_IMAGES" == 1 ]]; then
    deep-asteroids ingest images \
        --registry-uri "$REGISTRY_URI" --repo-dir "$REPO_DIR" \
        --product-type differenceExp --night "$NIGHT"
fi
if [[ "$BAND" == none ]]; then
    deep-asteroids ingest results --results-db "$RESULTS_DB" --night "$NIGHT"
else
    deep-asteroids search run --night "$NIGHT" --band "$BAND" --threshold "$THRESHOLD" --dx "$DX" \
        "${CATALOG_OPT[@]}" "${MASK_OPT[@]}"
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
