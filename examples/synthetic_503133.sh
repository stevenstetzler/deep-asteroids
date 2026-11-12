#!/usr/bin/env bash
# Reproduces the wide synthetic-binary example in PR #12 (issue #5):
# orbit_id 503133, a TNO binary with 4.0" separation (wide enough that
# the barycenter/photocenter distinction is visible by eye -- the
# photocenter sits closer to the brighter primary), night 20190708,
# field B0a, 121/121 exposures on-chip for both components.
#
# No --component given: `viz tracklet` renders all four
# (primary/companion/barycenter/photocenter) in one run, each into its
# own subdirectory under --out-dir.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

NIGHT=20190708
FIELD=B0a

setup_night "$NIGHT" "$FIELD" synthetic

deep-asteroids viz tracklet \
    --source synthetic --night "$NIGHT" --orbit-id 503133 \
    --out-dir "${OUT_DIR:-./viz/synthetic_503133}"
