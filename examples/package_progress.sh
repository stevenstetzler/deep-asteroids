#!/usr/bin/env bash
# The progress view for #32's batch backfill (package_backfill.sh):
# rendered/pending counts, per run_id and overall, for the same
# putative-real selection the backfill renders -- without rendering
# anything itself (`deep-asteroids package result --dry-run`). No separate
# tracking needed: the filesystem (each result's index.html) is the whole
# manifest, same as `package_result()` itself (#32's "no new tracking
# table"). Safe to run anytime, including against a backfill that's
# still running elsewhere.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${WEB_DIR:=web}"
: "${NIGHT:=}"
night_arg=()
[[ -n "$NIGHT" ]] && night_arg=(--night "$NIGHT")

run_ids=$(deep-asteroids query results "${night_arg[@]}" --putative-real --header \
    | awk -F'|' 'NR>1{print $1}' | sort -u)

total_rendered=0
total_pending=0
while IFS= read -r run_id; do
    [[ -z "$run_id" ]] && continue
    out=$(deep-asteroids package result --run-id "$run_id" --putative-real --dry-run --web-dir "$WEB_DIR")
    rendered=$(grep -oE '[0-9]+ already rendered' <<< "$out" | awk '{print $1}')
    pending=$(grep -oE '[0-9]+ pending' <<< "$out" | awk '{print $1}')
    printf '%-50s %6d / %-6d\n' "$run_id" "$rendered" "$((rendered + pending))"
    total_rendered=$((total_rendered + rendered))
    total_pending=$((total_pending + pending))
done <<< "$run_ids"

echo "---"
printf '%-50s %6d / %-6d\n' "total" "$total_rendered" "$((total_rendered + total_pending))"
