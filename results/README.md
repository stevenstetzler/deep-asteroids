# Results

Scripts that run against the full-survey database, not one validation night or a handful of
known objects (that's what `examples/` demos). `full_survey_summary.sh` (#15) builds that
database and its funnel/summary plots; `search_backfill_vr.sh` (#28) re-executes the full
survey's own searches pinned to `band='VR'`.

## Full-survey ingest and funnel (#15)

```bash
cd results
DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/$USER/deep_asteroids_full_survey.sqlite \
DEEP_DB=/path/to/cached/deep_database.sqlite \
./full_survey_summary.sh
```

From inside a Slurm job (not a login node): ingests and crossmatches the
whole survey (~277k results), real CPU work. `RESULTS_DB` (default salad's
local Postgres, `postgresql://$USER@localhost:55432/salad`) and `OUT_DIR`
(default `../docs/results`) can also be overridden. See the script's own
header comment for what each step does, and `docs/results/README.md` for
the funnel numbers and plot descriptions this run produced.

Idempotent: every `deep-asteroids` step it calls skips work already done
(`ingest`/`link`/`dedup`/`crossmatch`/`statistics` all check first), so
re-running after a partial failure only repeats what didn't finish.

## Re-executing searches pinned to band='VR' (#28)

```bash
cd results
DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/$USER/deep_asteroids_full_survey.sqlite \
DEEP_DB=/path/to/cached/deep_database.sqlite \
source /gscratch/dirac/DEEP/data/credentials \
./search_backfill_vr.sh
```

From a login node (it only materializes a run_id list and submits a Slurm job array; see
`docs/plans/PLAN_28_SEARCH_VR_REEXECUTION.md` for the full plan this implements). `search run`
owns its whole job's CPU budget per run_id (unlike the PDF backfill's `package result`), so
this is a job **array** -- one task per run_id, `$THROTTLE` (default 16) concurrent at a time
-- rather than the per-night `xargs` fan-out `package_backfill_putative_real.sh` uses.
`list_vr_run_ids.py` selects exactly the run_ids in scope: every `band='VR'` search definition
whose `(night, field, detector)` already has a searched `band IS NULL` one (the same searches
we have salad-copied results for), further restricted to ones whose every exposure already has
a `differenceExp` image ingested (`search run` has no partial-coverage tolerance -- a single
missing image fails the whole search outright). Checked against the real database: 4,261 of
5,572 in-scope searches qualify; the other 1,311 have a real image-coverage gap (not every
planned detector-exposure was actually observed/calibrated that night) and are skipped rather
than submitted as guaranteed failures.

Calibrated (12 real searches, 10 succeeded) at ~51.6s wall/search on 4 CPUs -- ~3.8h total wall
time and ~244 core-hours at the defaults. Preemptible/requeue-safe the same way the PDF
backfill is (`--requeue`, `--open-mode=append`, a restart-count log line per task); #37's
atomic-commit fix means a requeued or resubmitted task is always either fully done or not
started at all.
