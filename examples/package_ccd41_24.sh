#!/usr/bin/env bash
# Reproduces the one committed page of the static result site (#27):
# web/results/20190402/A0b/ccd41/VR/24/ -- the same result
# result_ccd41_24.sh renders with `viz result`, packaged instead with
# `deep-asteroids package` (index.html + report.pdf alongside the four
# plots). Shares that script's defaults, so another result works the
# same way:
#
#   NIGHT=20190403 FIELD=A0c DETECTOR=10 RESULT_NUM=3 ./package_ccd41_24.sh
#
# (the run is deep-decam-<FIELD>-ccd<DETECTOR>-<NIGHT>-<BAND>; override
# RUN_ID for another). Writes under $WEB_DIR (default ../web, i.e. the
# repo's own web/ -- package_result() derives the rest of the path from
# the search definition itself, so it always lands at
# web/results/<night>/<field>/ccd<detector>/<band>/<result_num>/).
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
: "${WEB_DIR:=../web}"

deep-asteroids ingest searches --night "$NIGHT"
deep-asteroids ingest images \
    --registry-uri "$REGISTRY_URI" --repo-dir "$REPO_DIR" \
    --product-type differenceExp --night "$NIGHT" --field "$FIELD"
deep-asteroids search run --run-id "$RUN_ID" --catalog-dir "$CATALOG_DIR" ${REMOVE_CATALOGS:+--remove-catalogs}

deep-asteroids package --run-id "$RUN_ID" --result-num "$RESULT_NUM" \
    --web-dir "$WEB_DIR" --annotate "$ANNOTATE" "$@"
