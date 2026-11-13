# Timing the full-survey ingest (#15)

Prerequisite work for #15 is merged into `main`: `ingest results` (#13/#14),
search infrastructure (#16/#20), dedup/crossmatch (#21/#22), MPC tracklet
linking (#23), and the per-result stats/filters/plots (#24). What remains
for #15 is to run that pipeline over the real full survey instead of a
single validation night, and publish summary plots under `docs/results/`.

## Step 1: how long does the full-survey ingest take?

Copying all of salad's results (5,573 runs) into a fresh
`deep_asteroids.sqlite` via:

```bash
deep-asteroids ingest results --results-db \
  "postgresql://localhost:55432/salad?host=<salad>/pgdata"
```

Run from inside an existing Slurm allocation (`compute-bigmem-astro`, 4 CPUs)
on klone; `ingest results` is a single-process DB copy, so no extra CPUs are
requested. Output written to a dedicated SQLite file in `/gscratch/dirac`
lab storage (D3 in #17's plan), separate from the dev/test database that
already holds night 20190402.

Timing: **27m35s real** (16m58s user, 24s sys; single process, 4 CPUs
allocated but not parallel) to ingest all 5,573 runs: 276,797 results,
22,454,995 gathered rows, 12,051,228 points, 276,797 tracklets. Output
database is 2.87 GB, in line with #17's ~3.5 GB/~1 hour estimate (faster
in practice).

**Blocker found:** all 5,573 run_ids ingested with no matching
`search_definition` row (`ingest searches` has only been run for the
validation night/field so far, not the full survey). This is not just
metadata: `crossmatch/match.py`, `crossmatch/dedup.py` and
`crossmatch/completeness.py` all join through `search_definition` /
`SearchDefinitionExposure` to get each exposure's time and chip geometry,
so the crossmatch and dedup steps needed for the summary plots will not
run correctly against this data as-is. Next step before the summary
plots: run `ingest searches` across the full survey (same pattern as the
images ingest — scoped per `(night, field)` pointing group), then
re-check `search_definition` coverage.

