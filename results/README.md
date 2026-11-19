# Results

Scripts that run against the full-survey database, not one validation night or a handful of
known objects (that's what `examples/` demos). Four things live here: `full_survey_summary.sh`
(#15), which builds that database and its funnel/summary plots; `search_backfill_vr.sh` (#28),
which re-executes the full survey's own searches pinned to `band='VR'`;
`package_backfill_putative_real.sh` (#32/#33), which renders PDFs from it; and `merge_pdfs.sh`
(#32/#33), which merges those PDFs by group.

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

## Batch-rendering putative-real PDFs (#32/#33)

```bash
cd results
DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/$USER/deep_asteroids_full_survey.sqlite \
DEEP_DB=/path/to/cached/deep_database.sqlite \
source /gscratch/dirac/DEEP/data/credentials \
./package_backfill_putative_real.sh
```

From a login node (it only submits `sbatch` jobs, does no rendering itself):
submits one `sbatch` job per night that has at least one putative-real
result at `sigma_v < 0.00015` (`$SIGMA_V_THRESHOLD` overrides), each
rendering that night's results (`deep-asteroids package result
--putative-real --no-movie` -- no movies, to keep storage down: see PR #33)
across `$CORES` (default 16) workers on `-p ckpt-all -A ckpt-astro`
(`$PARTITION`/`$ACCOUNT` override -- ckpt is preemptible, cheap, and this
workload doesn't need a dedicated allocation), plus one final `package
index` job depending on all of them. See the script's own header comment
for the full option list, and PR #33 for the runtime/storage estimate
behind the defaults.

Also preemptible- and requeue-safe: ckpt jobs get killed and automatically
restarted (`PreemptMode=REQUEUE`, plus `--requeue` passed explicitly) under
the same job ID, logs preserved across the restart (`--open-mode=append`);
`package result`'s own skip-if-`index.html`-exists check is the whole
manifest either way (#32's "no new tracking table"), so a restart,
requeue, or resubmission only (re)does whatever didn't finish --
`index.html` itself is written via a temp file + atomic rename, so a
mid-render kill never leaves a truncated one behind that a later run
would mistake for "done".

## Merging PDFs by group (#32/#33)

```bash
cd results
DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/$USER/deep_asteroids_full_survey.sqlite \
./merge_pdfs.sh
```

From a login node -- unlike the backfill, this is cheap (concatenating already-rendered PDFs,
no subprocess engine), so no Slurm job is needed. Merges every night's rendered per-result PDFs
into one PDF per `(night, group)` under `$WEB_DIR/pdfs/<night>/<group>.pdf` -- **mpchecker**,
**synthetic**, and **putative-new**, the three groups `docs/results/magnitudes.png` highlights
(see the main README's "Merged PDFs by group" section for what each means). Kept per-night
rather than one giant PDF per group across the whole survey, to keep file sizes manageable.
Extra arguments (e.g. `--night`, `--group`, `--force`) pass straight through to
`deep-asteroids package merge-pdfs`; run it again after a backfill resumes or finishes more
nights and it only (re)does `(night, group)` pairs that changed or weren't done yet (same
cache-by-output-file-existence convention as `package result`).

**Night 20190401 has no merged PDFs at all (#38), and that's correct, not a bug.** It has
normal amounts of ingested metadata (496 search definitions, 3,050 results, all with
statistics/dedup/crossmatch already run), but only 149 `Image` rows ingested survey-wide for
the whole night (vs. ~7,000 for neighboring nights 20190402/20190403) -- `ingest images`
genuinely found almost no calibrated `differenceExp` products for it in the Butler registry.
With no images, forced photometry finds nothing to measure at any predicted position, so
`package result` renders `sky.png` (needs only the DB) but no coadd/light-curve/PDF for any
result that night, and `merge-pdfs` correctly finds zero rendered PDFs to merge and skips it
(its own "no empty file" rule, not a new check). If this night's data coverage improves
upstream, re-running `package_backfill_putative_real.sh`/`merge_pdfs.sh` picks it up with no
code changes needed.

## Missed submitted (mpc_linked) tracklets (#40)

Two scripts that answer why findable submitted tracklets were missed, run from a compute node against
the full-survey database (both read it read-only; see each script's docstring for its arguments):

- `mpc_linked_velocity_check.py` takes every mpc_linked tracklet whose track crosses a searched chip
  and records whether its speed is inside a search's velocity range and whether a result matched it
  (within 2"). Over the survey: 44,102 on searched chips, of which 41,972 are in range, and **all 4,519
  that were missed are in range** (velocity range is not the cause). About an hour.
- `missed_mpc_linked_observations.py` turns its missed in-range tracklets into
  `missed_mpc_linked_observations.csv`: night, field, the CCD(s) the observations landed on, the searched
  chip, provid/permid/name (3,017 of 4,519 are undesignated), and each tracklet's observation count and obsids,
  taken from the merged tracklet's member trkids and matched into deep-db on `(trkid, trksub)`.

## Search parity between the legacy and VR searches (#28)

`legacy_vs_vr_parity.py` maps each legacy search's salad image ids (salad's Postgres must be running) to the exact
(exposure, chip) salad searched, and compares that with the exposures the band='VR' search can search now. Over the
survey, all 5,568 legacy runs with results have the same exposures (435,908 in total) and read the same image files,
so there is nothing salad searched that a VR search cannot. 1,245 of those searches cover fewer exposures than their
search definition because the Butler registry has no differenceExp for the rest; salad skipped the same ones, and
`search run` now does too (see `search_missing_image`).
