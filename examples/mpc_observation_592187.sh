#!/usr/bin/env bash
# Reproduces the mpc_observation example in PR #12 (issue #5): 2014 QZ79
# (permid 592187), night 20201019, field B1d, trkid 00000FFqBy (88/88
# observations on-chip). deep-db also has a second known_tracklet row
# for this same object that night, trkid 00000FFq8F -- flagged
# same_obstime_conflict (no fit), so it's skipped here by naming the
# trkid directly; `--permid 592187` would resolve both (the flagged one
# correctly reports "0 exposure(s) touched", not a failure, but then
# the two share --out-dir and get nested id-numbered subdirectories
# instead of this script's own clean path).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

NIGHT=20201019
FIELD=B1d

setup_night "$NIGHT" "$FIELD" mpc-observations

deep-asteroids viz tracklet \
    --source mpc_observation --night "$NIGHT" --trkid 00000FFqBy \
    --out-dir "${OUT_DIR:-./viz/mpc_observation_592187}" --annotate "$ANNOTATE"
