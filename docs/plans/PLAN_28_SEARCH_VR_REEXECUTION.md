# Plan: re-execute the full survey's searches, pinned to band='VR' (#28)

## What #28 asks for

Re-run `deep-asteroids search run` -- our own detect/search/ingest pipeline, already
validated against salad's own search for one CCD (#16) -- over every `(night, field,
detector)` unit we currently have *salad-copied* results for, but this time pinned to
`band='VR'` so the new runs are a distinct, clearly-labelled batch rather than overwriting
or mixing with the copied-in ones. Then diff the two: "see if anything major has gone
wrong" is #28's own acceptance bar, not a specific metric.

## Scope, measured against the real full-survey database

```
(night, field, detector) triples with salad-copied (band=NULL) results: 5,572
  ... of which already have a band='VR' search_definition ingested:      5,572  (100%)
  ... of which have ever been searched under that VR run_id:                 0
```

So the target set is exactly defined and already fully prepared on the ingest side
(`ingest searches` already wrote every band's `search_definition` row, VR included) --
this is purely a `search run` batch, no new ingest step needed first. Every `run_id` in
scope follows `_run_id()`'s existing convention
(`ingest/searches.py`): `deep-decam-<field>-ccd<detector>-<night>-VR`, distinct by
construction from the un-suffixed `deep-decam-<field>-ccd<detector>-<night>` the salad
copy lives under.

## Lessons from #32/#33 (the PDF backfill) -- what transfers and what doesn't

The PDF backfill hit two real bugs before it ran cleanly, and both generalize:

1. **SQLite is a single-writer store, and its default `busy_timeout` is 0** --
   `database/__init__.py`'s `create_engine(url, echo=echo)` passes no `connect_args`, so a
   second writer doesn't wait, it fails immediately with `sqlite3.OperationalError:
   database is locked`. That's what broke concurrent `ingest images` calls, and
   `run_search_for_definition()`'s own `ingest_results_dir()` commit is exactly the same
   kind of write, just smaller per call (one search's `Search`/`Result`/`Tracklet`/
   `Gathered` rows, not a whole night's `Image` rows). **Proposed prerequisite fix** (small,
   should land as its own PR before the batch runs): set a SQLite busy timeout via
   `connect_args={"timeout": N}` (N large enough to cover the slowest realistic commit under
   the batch's expected concurrency -- see "Calibrate before sizing" below) so concurrent
   writers queue instead of erroring. This is a strictly better fix than the PDF backfill's
   "pull the one write into its own serial job" workaround -- it was only necessary there
   because dozens of jobs all wrote at the exact same moment at job start; here, a timeout
   is enough because (see below) the batch's own concurrency is naturally bounded.

2. **A result can be left in an ambiguous "skip" state by a non-atomic commit.**
   `render_result_page()`'s `index.html` write had the same shape of bug we're about to
   avoid here: `run_search_for_definition()` checks `existing = ... Search.run_id ==
   run_id ...; if existing is not None and not force: return` to decide whether a run_id is
   already done -- but `ingest_results_dir()` commits the `Search`/`Result`/`Tracklet`/
   `Gathered` rows in one transaction, and then `run_search_for_definition()` does a
   **second, separate** `session.commit()` afterwards just for the `SearchRun` bookkeeping
   row. A process killed between those two commits (preemption, OOM, anything) leaves a
   `Search` row with no `SearchRun` row -- and the idempotency check only looks at `Search`,
   so every future re-run would skip that run_id forever, silently missing its `SearchRun`
   provenance (seed/timing/versions) permanently. **Proposed prerequisite fix**: merge the
   two commits into one (move the `SearchRun` insert before `ingest_results_dir()`'s own
   commit, or have `run_search_for_definition()` manage one transaction end to end) so a
   run_id is either fully done -- `Search` + its results + its `SearchRun` row, all
   together -- or not started at all, same all-or-nothing guarantee the PDF backfill now has
   for `index.html`.

3. **What does *not* transfer directly: the PDF backfill's "one `sbatch` job per night,
   fan out across run_ids inside it with `xargs -P`" shape.** That worked because
   `package result` is single-threaded per call -- fanning many of them out inside one job
   just uses the job's cores, one call each. `search run` is the opposite: it's written to
   *own* the whole job's CPU budget for **one** run_id --
   `detect_exposures(..., workers=workers)` defaults `workers` to `allocated_cpus()`, and
   `run_search_for_definition()` itself calls
   `numba.set_num_threads(min(allocated_cpus(), ...))` before the search/refinement step.
   Fanning several run_ids out inside one job with `xargs -P` would make each one ask numba
   for the *entire* job's cores, oversubscribing badly (exactly the kind of thing the
   cluster policy on thread counts warns against) -- unless workers/numba threads were
   explicitly divided down, which the current code doesn't do.

   The natural fit here, and what #32's own writeup flagged as the alternative to the
   per-night grouping ("...or an sbatch array job"), is a **Slurm job array**: one task per
   run_id (5,572 tasks), each a small allocation (a handful of cores, sized to what one
   `search run` call actually uses -- see calibration), throttled with `--array=1-5572%N`
   so only `N` tasks run at once. `N` does double duty: it bounds how many nodes/cores the
   batch asks for at a time (resource-honesty, same principle as the PDF backfill's
   `$CORES`), *and* it bounds how many concurrent SQLite writers the busy-timeout fix above
   actually has to absorb -- pick `N` small enough that a `busy_timeout` on the order of
   a minute or two is comfortably enough for `N` queued commits of a few seconds each.

4. **Preemption/resume safety reuses directly.** Once the atomic-commit fix above lands,
   `run_search_for_definition()`'s existing `existing is not None and not force: return`
   check is exactly `package_result()`'s skip-if-done cache, no new tracking table needed --
   a requeued or resubmitted array task just re-checks and skips if done. Reuse the same
   belt-and-suspenders the PDF backfill settled on: `--requeue` passed explicitly at
   submission (`ckpt-all`'s own `PreemptMode=REQUEUE` should already cover it, but costs
   nothing to also ask), `--open-mode=append` on each task's log so a requeue doesn't erase
   the previous attempt's output, and a restart-count line logged at the top of each task.

5. **Don't trust one calibration number.** The PDF backfill's storage estimate was wrong
   by ~3x the first time (GPFS's 8MB block size inflating small-file storage well past the
   sum of file sizes) because it leaned on a single example. The only real `search_run` row
   anywhere right now (the local dev database's `deep-decam-A0b-ccd41-20190402-VR`, 103
   exposures) shows `detect_seconds=0.03`, `search_seconds=9.28`, `ingest_seconds=2.81` at
   4 CPUs -- but that `detect_seconds` is almost certainly a catalog-cache hit (detection
   skips an exposure whose catalog file already exists), not a real cold-detect timing, so
   it understates the real cost. **Before sizing the real batch, calibrate**: run `search
   run --band VR` for a deliberately cold sample (a few runs per night across several
   different nights, `--force` or a catalog_dir the sample has never touched) and use *that*
   set of real `search_run.detect_seconds`/`search_seconds`/`ingest_seconds` rows, not the
   one cached number, the same way PR #33 was corrected once real numbers came in from the
   actual run.

6. **Keep generated output out of git.** The PDF backfill generated tens of thousands of
   files under `web/`; none of that was committed except the one pre-existing example
   `.gitignore` already special-cases. The equivalent risk here is catalog files and the
   growing `deep_asteroids.sqlite`/full-survey database itself -- neither belongs in git.
   `search run`'s own `--catalog-dir` already defaults to `/gscratch/scrubbed/$USER/...`
   (scrubbed, outside the repo); keep it there (or another non-repo path), and remember
   scrubbed storage is deleted after ~21 days of inactivity -- if the batch runs long or
   gets paused, make sure the catalog files it still needs stay "active" (don't let this
   batch stretch across that window) or point `--catalog-dir` at group storage instead.

## Concrete plan

1. **Prerequisite PR (small, no search behavior change)**: add a SQLite busy timeout
   (`connect_args={"timeout": N}` in `database/__init__.py`'s `create_engine()` call, both
   call sites) and merge `run_search_for_definition()`'s two commits into one. Add a test
   for the atomicity fix (interrupt between the two inserts -- or restructure so there's
   only one insert boundary to test). This is the direct, narrow fix for lessons #1/#2
   above, useful independent of this batch (it already matters for `search_ccd41.sh` and
   any other caller).

2. **Calibrate.** Pick ~10-20 run_ids spanning a handful of different nights/fields (to
   sample varying exposure counts/detector positions), run `deep-asteroids search run
   --run-id <id> --band VR` for each with a clean catalog dir, and read back their real
   `search_run` rows. Use those for the actual runtime estimate (core-hours, wall clock at
   a chosen array throttle `N`) and a storage estimate (catalog file footprint + DB growth),
   posted to the tracking PR/issue the same way PR #33 posted and then corrected its
   estimate -- numbers here before committing to the full run, not after.

3. **Batch driver**: `results/search_backfill_vr.sh` (naming to match
   `package_backfill_putative_real.sh`), structured as:
   - enumerate the 5,572 target run_ids (every `search_definition` with `band='VR'` whose
     `(night, field, detector_number)` also has an ingested `band IS NULL` `Search` row --
     the same query used for the scope count above);
   - submit one `sbatch --array=1-<n>%<throttle>` job, each array task resolving its own
     run_id from `$SLURM_ARRAY_TASK_ID` against a materialized list file, then calling
     `deep-asteroids search run --run-id "$RUN_ID" --band VR --catalog-dir <scrubbed path>
     --remove-catalogs` (dropping each run's catalogs once ingested, like
     `package_ccd41_24.sh` already does for its one example, to keep scratch usage bounded
     over 5,572 runs);
   - `--requeue`, `--open-mode=append`, and a restart-count log line, matching #32/#33;
   - sized (`-c`) from the calibration step, not guessed, and `$THROTTLE` (the array's `%N`)
     exposed the same way `$CORES`/`$PARTITION`/`$ACCOUNT` are in the existing script, so a
     human can tune queue pressure without editing the script.
   - idempotent resubmission the same way: Slurm array jobs natively skip nothing on
     resubmission, so a second `sbatch --array=...` run would redo *already-done* tasks
     uselessly (if cheap, a fast skip, same no-op package_result's cache already makes
     safe) -- but check for an existing array job under the same name first anyway, as
     package_backfill_putative_real.sh does, to avoid doubling concurrency.

4. **Validate** ("diff the search plots," #28's own bar), two ways:
   - **Per-result, structural**: `examples/compare_search.py NEW_RUN_ID OLD_RUN_ID` already
     exists and already does exactly this (#16) -- L0 detector through L3 outputs, reported
     as counts and difference sizes, not pass/fail. Run it for every
     `(VR run_id, band=NULL run_id)` pair (or a large random sample if 5,572 pairs' worth of
     `--catalog-dir`-dependent L0 comparisons is too slow) and tally how many pairs show a
     material difference at each level -- this is the direct "did anything go wrong"
     check, and it's already been proven at the single-CCD scale.
   - **Aggregate, statistical**: regenerate the `docs/results/`-style population plots
     (`sigma_v`/`residuals`/`magnitudes`/`completeness`) scoped to just the new VR run_ids,
     and compare by eye against the existing full-survey ones (`results/full_survey_summary.sh`'s
     own output) -- the funnel should land in the same neighborhood (same objects, same
     cuts) if the re-execution reproduced the original search's substance, even though the
     exact result ordering/numbering can differ run to run.

## Open questions (for discussion before implementation)

- Should the calibration + prerequisite-PR step happen before the full batch is submitted,
  or is the author comfortable sizing more loosely given how well #16 and #32/#33 already
  validated the underlying pieces?
- Any reason the original full-survey searches never covered individual bands (as opposed
  to the union)? Worth confirming VR is the intended choice band-for-band (the DB also has
  `r`/`i`/`Y` `search_definition` rows already ingested) rather than, say, wanting all three
  non-VR bands done too eventually.
- Should `--remove-catalogs` be used by default (keeps scratch bounded, matches the existing
  example script) or kept around for the validation step's `compare_search.py --catalog-dir`
  L0 check, which needs the catalogs to still exist?
- Array throttle `N`: pick a number now, or make the calibration step's own measured
  DB-commit duration drive it (the plan above ties them together, but a concrete starting
  number -- e.g. 16, matching `$CORES`'s existing default elsewhere in this repo -- might be
  worth just stating up front)?
