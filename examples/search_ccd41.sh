#!/usr/bin/env bash
# Reruns one find-asteroids search and compares it with salad's earlier run
# (issue #16): band VR, night 20190402, field A0b, detector 41 -- the CCD
# whose result 24 is the example in result_ccd41_24.sh. Override any of
# NIGHT/FIELD/DETECTOR/BAND to try another search that salad also ran:
#
#   NIGHT=20190403 FIELD=A0c DETECTOR=10 ./search_ccd41.sh
#
# The previous run is copied from $RESULTS_DB (default salad's Postgres on
# localhost:55432 -- start it first) under its own run_id
# deep-decam-<FIELD>-ccd<DETECTOR>-<NIGHT>; the rerun lands under the
# search definition's run_id, with -<BAND> appended.
#
# What it does:
#   1. ingests the night's search definitions, its images, and the old run;
#   2. `search run` (detects the 103 images, writing catalogs under
#      $CATALOG_DIR, then searches and ingests);
#   3. compares the rerun with the old run, level by level (compare_search.py);
#   4. measures the refinement stage's noise floor: reruns the same search with
#      two other seeds in copies of the database (the catalogs are reused) and
#      compares each with the first. The old run's refinement drew from an
#      unseeded RNG, so old-vs-new differences of the same size as
#      seed-to-seed ones are expected, not a failure.
#
# The report is printed; set $REPORT to a file to keep a copy too.
# Run it from a Slurm job: it detects and searches (sized from the
# allocation), and needs the cutout/detect engines built (see the README).
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

case "$DEEP_ASTEROIDS_DB" in
    sqlite:///*) DB_FILE="${DEEP_ASTEROIDS_DB#sqlite:///}" ;;
    *) echo "this example needs a SQLite \$DEEP_ASTEROIDS_DB (it copies the database)" >&2; exit 2 ;;
esac

deep-asteroids ingest searches --night "$NIGHT"
deep-asteroids ingest images \
    --registry-uri "$REGISTRY_URI" --repo-dir "$REPO_DIR" \
    --product-type differenceExp --night "$NIGHT" --field "$FIELD"
deep-asteroids ingest results --results-db "$RESULTS_DB" --run-id "$OLD_RUN_ID"

SEARCH=(deep-asteroids search run --run-id "$NEW_RUN_ID" --catalog-dir "$CATALOG_DIR")
"${SEARCH[@]}"

{
    echo "== ${NEW_RUN_ID} (seed 0) vs the previous run ${OLD_RUN_ID}"
    python compare_search.py "$NEW_RUN_ID" "$OLD_RUN_ID" \
        --catalog-dir "$CATALOG_DIR"
    for seed in 1 2; do
        echo
        echo "== noise floor: seed ${seed} vs seed 0 (same catalogs, same everything else)"
        cp "$DB_FILE" "${DB_FILE}.seed${seed}"
        DEEP_ASTEROIDS_DB="sqlite:///${DB_FILE}.seed${seed}" "${SEARCH[@]}" --seed "$seed" --force > /dev/null
        python compare_search.py "$NEW_RUN_ID" "$NEW_RUN_ID" --new-db "sqlite:///${DB_FILE}.seed${seed}"
        rm -f "${DB_FILE}.seed${seed}"
    done
} 2>&1 | grep -v "^INFO\|^WARNING" | if [ -n "${REPORT:-}" ]; then tee "$REPORT"; else cat; fi
