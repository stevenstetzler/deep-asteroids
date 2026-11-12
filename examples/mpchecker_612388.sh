#!/usr/bin/env bash
# Reproduces the mpchecker-ephemeris example in PR #12 (issue #5): 2002
# PV170 (permid 612388), night 20220825, field B1e -- 93 predicted
# positions, 92 of them on-chip and all with full cutouts/forced
# photometry once this finishes.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

NIGHT=20220825
FIELD=B1e

setup_night "$NIGHT" "$FIELD" mpc-ephemeris

deep-asteroids viz tracklet \
    --source mpchecker --night "$NIGHT" --permid 612388 \
    --out-dir "${OUT_DIR:-./viz/mpchecker_612388}"
