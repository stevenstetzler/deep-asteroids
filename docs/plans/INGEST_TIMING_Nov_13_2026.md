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

Timing: *(filled in once the run finishes)*

