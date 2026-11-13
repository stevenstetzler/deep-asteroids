#!/usr/bin/env bash
# Reproduces the find-asteroids result example in PR #14 (issue #13):
# result 24 of deep-decam-A0b-ccd41-20190402 -- a real-looking mover
# (stacked forced-photometry SNR ~100, mean mag ~23.4) from salad's
# search of night 20190402, field A0b, CCD 41.
#
# Ingests that night's search definitions, its images, and salad's
# results for it, then measures and renders the result. The same script
# works for another result by overriding any of:
#
#   NIGHT=20190403 FIELD=A0c DETECTOR=10 RESULT_NUM=3 ./result_ccd41_24.sh
#
# (the find-asteroids run_id is deep-decam-<FIELD>-ccd<DETECTOR>-<NIGHT>;
# override RUN_ID for a differently named one). Output images are stored
# in docs/examples/viz/result_<NIGHT>_ccd<DETECTOR>_<RESULT_NUM>/ unless
# $OUT_DIR says otherwise.
#
# $RESULTS_DB is the find-asteroids results database to copy from
# (default: salad's Postgres on localhost:55432 -- start it first). The
# default URL names $USER explicitly: the registry credentials this
# needs export PGUSER=deep_readonly, which libpq would otherwise apply to
# a URL without a user. $SHUTTER_OFFSET_SECONDS (default 0.5) is the shutter
# latency salad's detection engine adds to each exposure's time; it is 0
# for results from a search that does not add it (see viz_result.py).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

: "${NIGHT:=20190402}"
: "${FIELD:=A0b}"
: "${DETECTOR:=41}"
: "${RESULT_NUM:=24}"
: "${RUN_ID:=deep-decam-${FIELD}-ccd${DETECTOR}-${NIGHT}}"
: "${RESULTS_DB:=postgresql://${USER}@localhost:55432/salad}"
: "${SHUTTER_OFFSET_SECONDS:=0.5}"

deep-asteroids ingest searches --night "$NIGHT"
deep-asteroids ingest images \
    --registry-uri "$REGISTRY_URI" --repo-dir "$REPO_DIR" \
    --product-type differenceExp --night "$NIGHT" --field "$FIELD"
deep-asteroids ingest results --results-db "$RESULTS_DB" --run-id "$RUN_ID"

python viz_result.py "$RUN_ID" "$RESULT_NUM" \
    "${OUT_DIR:-../docs/examples/viz/result_${NIGHT}_ccd${DETECTOR}_${RESULT_NUM}}" \
    --annotate "$ANNOTATE" --shutter-offset-seconds "$SHUTTER_OFFSET_SECONDS"
