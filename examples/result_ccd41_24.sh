#!/usr/bin/env bash
# The find-asteroids result example (PRs #14 and #20): result 24 of
# deep-decam-A0b-ccd41-20190402-VR, a real-looking mover (stacked
# forced-photometry SNR ~100, mean mag ~23.4), from OUR OWN search of night
# 20190402, field A0b, CCD 41, band VR -- the same seeded search
# search_ccd41.sh compares with salad's earlier run (the same parameters and
# seed), not salad's results themselves: result 24 comes out identical either
# way.
#
# Ingests that night's search definitions and images, runs the search
# (`search run`: detects the images into $CATALOG_DIR, searches, ingests),
# then measures and renders the result. The same script works for another
# result by overriding any of:
#
#   NIGHT=20190403 FIELD=A0c DETECTOR=10 RESULT_NUM=3 ./result_ccd41_24.sh
#
# (the run is deep-decam-<FIELD>-ccd<DETECTOR>-<NIGHT>-<BAND>; override
# RUN_ID for another). Output images are stored in
# docs/examples/viz/result_<NIGHT>_ccd<DETECTOR>_<RESULT_NUM>/ unless
# $OUT_DIR says otherwise.
#
# Run it from a Slurm job with the detect engine built (see the README).
# Set $REMOVE_CATALOGS to delete the search's catalogs afterwards.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

: "${NIGHT:=20190402}"
: "${FIELD:=A0b}"
: "${DETECTOR:=41}"
: "${RESULT_NUM:=24}"
: "${BAND:=VR}"
: "${RUN_ID:=deep-decam-${FIELD}-ccd${DETECTOR}-${NIGHT}-${BAND}}"
: "${CATALOG_DIR:=/gscratch/scrubbed/${USER}/deep_asteroids/catalogs}"

deep-asteroids ingest searches --night "$NIGHT"
deep-asteroids ingest images \
    --registry-uri "$REGISTRY_URI" --repo-dir "$REPO_DIR" \
    --product-type differenceExp --night "$NIGHT" --field "$FIELD"
deep-asteroids search run --run-id "$RUN_ID" --catalog-dir "$CATALOG_DIR" ${REMOVE_CATALOGS:+--remove-catalogs}

python viz_result.py "$RUN_ID" "$RESULT_NUM" \
    "${OUT_DIR:-../docs/examples/viz/result_${NIGHT}_ccd${DETECTOR}_${RESULT_NUM}}" \
    --annotate "$ANNOTATE"
