#!/usr/bin/env bash
# De-duplicates and cross-matches one search's results (issue #21): the seeded
# band-VR search of night 20190402, field A0b, detector 41 that search_ccd41.sh
# reproduces, against the known objects of the same night -- submitted MPC
# observations, mpchecker ephemerides and synthetic objects. Override
# NIGHT/FIELD/DETECTOR/BAND to try another search.
#
# What it does:
#   1. ingests the night's search definitions, its three known-tracklet
#      sources and its images, links the MPC tracklets split across trkids
#      (`link`), then runs the search (`search run`);
#   2. `dedup`: clusters redetections of the same object among the results;
#   3. `crossmatch`: matches every result against the night's known tracklets,
#      and prints how many results match each kind of object;
#   4. lists the closest match of each source for each matched result;
#   5. compares with salad's own crossmatch of the same search, which is
#      copied from $RESULTS_DB (default salad's Postgres on localhost:55432 --
#      start it first) so the two runs can be paired by result number, and
#      also clusters our run together with salad's, which finds each result
#      twice (a check that redetections are merged).
#
# Run it from a Slurm job with the detect engine built (see the README).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

: "${NIGHT:=20190402}"
: "${FIELD:=A0b}"
: "${DETECTOR:=41}"
: "${BAND:=VR}"
: "${RESULTS_DB:=postgresql://${USER}@localhost:55432/salad}"
: "${CATALOG_DIR:=/gscratch/scrubbed/${USER}/deep_asteroids/catalogs}"
OLD_RUN_ID="deep-decam-${FIELD}-ccd${DETECTOR}-${NIGHT}"
NEW_RUN_ID="${OLD_RUN_ID}-${BAND}"

deep-asteroids ingest searches --night "$NIGHT"
deep-asteroids ingest tracklets synthetic mpc-observations mpc-ephemeris --night "$NIGHT"
deep-asteroids link --night "$NIGHT"
deep-asteroids ingest images \
    --registry-uri "$REGISTRY_URI" --repo-dir "$REPO_DIR" \
    --product-type differenceExp --night "$NIGHT" --field "$FIELD"
deep-asteroids search run --run-id "$NEW_RUN_ID" --catalog-dir "$CATALOG_DIR"

echo; echo "== de-duplication (our run alone)"
deep-asteroids dedup --run-id "$NEW_RUN_ID"

echo; echo "== cross-match"
deep-asteroids crossmatch --run-id "$NEW_RUN_ID"

echo; echo "== the closest match of each source, per matched result"
deep-asteroids query crossmatches --run-id "$NEW_RUN_ID" --max-mean-sep-arcsec 10 --header | column -s'|' -t

echo; echo "== against salad's crossmatch of the same search"
deep-asteroids ingest results --results-db "$RESULTS_DB" --run-id "$OLD_RUN_ID"
python compare_crossmatch.py "$NEW_RUN_ID" "$OLD_RUN_ID" --results-db "$RESULTS_DB"

echo; echo "== de-duplication across both runs (each object was found twice)"
deep-asteroids dedup --force
deep-asteroids dedup --force --run-id "$NEW_RUN_ID" > /dev/null  # leave our run clustered on its own
