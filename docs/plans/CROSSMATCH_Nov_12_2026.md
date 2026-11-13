# Plan for issue #15: ingest the full survey's search results, then crossmatch

Issue #15: "ingest the full survey search results; crossmatch result
tracklets to submitted observations, known object ephemerides, and
synthetic object ephemerides."

This is a plan, for review before any code. Items marked **DECISION** need
the owner's call; each has a proposed default. Numbers for salad come from
its live Postgres (`localhost:55432/salad`) on 2026-11-12.

## What already exists

- `search_definition`/`search_definition_exposure`: one row per
  `(night, field, detector[, band])`, with each exposure's midpoint and
  chip footprint (#7).
- `known_tracklet`: one linear fit per known object per night, for the
  three sources the issue names (#3/#4):
  - `mpc_observation` = submitted observations (`statuses` records the
    MPC `status` values I/P/p),
  - `mpchecker` = known-object ephemerides,
  - `synthetic` = synthetic (fake) objects, with binary
    `barycenter`/`photocenter` rows.
  Times are MJD TAI; `vra` is plain dRA/dt (no cos dec); `ra_0`/`dec_0`
  are the MJD-0 convention, the same as find-asteroids' `tracklet` table.
- `image`: Butler paths for every detector-exposure (#9).
- `ingest results` (#13): copies one run, a night, or everything from a
  find-asteroids results database, idempotent per run_id. Verified on
  night 20190402 (60 runs, ~45 s).

So both halves of the crossmatch already live in one database, and a
crossmatch needs neither deep-db nor DuckDB: it is a join between
`result`/`tracklet` and `known_tracklet`, predicted at the search's own
exposure times.

## Salad's baseline (what we are porting and what to compare to)

| | salad | rows |
|---|---|---|
| `search` | one per `(field, ccd, night)` | 5,573 (nights 20190401-20220827; 62 nights; one non-standard run_id, `deep-B1b-ccd20-20190827`) |
| `result` / `tracklet` | find-asteroids output | 276,797 each |
| `gathered` / `points` | | 22,454,995 / 12,051,228 |
| `result_dedup_membership` | redetection merges (within a run, then across CCDs of a pointing group) | 276,747 |
| `crossmatch` | the closest candidate per *authoritative* result | 275,077: fake 151,715, super_tracklet 77,734, mpchecker 45,628 |

For the pilot night 20190402, salad has 3,003 results (2,997 authoritative);
best-candidate sources: fake 1,741, super_tracklet 697, mpchecker 559 (of
which 169, 258 and 129 have mean separation < 2"). `super_tracklet` is
salad's merged form of MPC observations (`mpc_link.py`).

Code to port (read-only reference, not copied wholesale):

| deep-asteroids step | salad reference |
|---|---|
| crossmatch core: predict both tracklets at the exposure times, mean/min/max separation | `crossmatch.py` (`crossmatch_result`, `sep_arcsec`, `velocity_angle`), `tracklet_predict.py` |
| redetection merge | `dedup.py`, `result_merge.py`, `crossmatch.cluster_fits` |
| per-result residual statistic | `result_stats.py` |
| match-type classification, the "putative new" cut, funnel counts | `scripts/summarize_results.py` |
| plots | `scripts/plot_residual_by_group.py`, `plot_residual_by_subgroup.py`, `plot_median_magnitude.py`, `plot_fake_completeness_magnitude.py`, `viz.py::render_sky_plot` (done in #14) |

What does **not** carry over: the DuckDB-attached Postgres bridge, the
`gathered -> image -> exposure` join hop, per-result candidate fetching
from deep-db, and refitting MPC/ephemeris points per run. Those existed
only to compensate for deep-db access; `known_tracklet` is already fit.

## Decisions

**D1. Time convention for salad's results (resolved; see #18).** An
earlier version of this plan assumed salad's result times are 32.184 s
early. They are not: synthetic objects recovered on 20190402 match the
results' tracklets to a median 0.03" with an implied time offset of
-0.18 +/- 0.22 s. The only offset is the 0.5 s DECam shutter label salad's
detection engine adds. So a result is evaluated at an exposure's deep-db
UTC midpoint converted to TAI, plus 0.5 s, with no rewrite of the data. The
0.5 s is the one per-source constant; a native deep-asteroids search that
does not add it uses 0. It is stored per run, in a small `results_ingest`
provenance table (run_id, source database, ingest time,
`shutter_offset_seconds`), so the crossmatch reads it instead of
hard-coding salad.

**D2. Is redetection dedup in scope?** The issue lists only ingest and
crossmatch, but without dedup one real object appears in many adjacent
search cells, and every crossmatch count (matched, unmatched, "new") is
inflated. Salad crossmatches only authoritative results. Proposed:
include it as its own step (3 below), after the crossmatch itself works on
raw results, so the crossmatch can be validated on its own first. If you
would rather split it into a follow-up issue, steps 4-5 only need the
`authoritative` flag to default to true.

**D3. Where the full database lives.** The full survey is ~22.5M
`gathered` + ~12M `points` rows. By the 20190402 file (41 MB for 293k
gathered + 169k points) that is **~3.5 GB of SQLite**, versus 39 GB for
salad's Postgres (which includes its other tables). SQLite is fine for one
writer, but note the group quota: `/gscratch/dirac` is at ~95% of its
fileset (252.5 / 266.2 TB), so `hyakstorage` must be checked before the
ingest, and the file kept in the lab's `/gscratch` space (not scrubbed).
Proposed: keep SQLite, one file, written by a single process; revisit
Postgres only if the crossmatch tables (below) make concurrent writers
necessary.

**D4. "Submitted observations".** Proposed: every `mpc_observation`
`known_tracklet` (statuses I, P and p), since the deep-db table is exactly
what was submitted/reported. Salad merged split trksubs into one
`super_tracklet` first. We do not have that merge yet (it is the
phase-2 `linked_tracklet` step in the migration plan), so a split object
will match several rows. The crossmatch records `trkid`/`provid` on the
match, so rows can be grouped by object when counting; a real merge stays a
separate issue.

**D5. Thresholds.** Defaults copied from salad's summary: known radius
2.0" for submitted and synthetic matches, 10.0" for mpchecker, candidate
cut 30" mean separation. All are command-line options.

## Steps

Each step is one PR (or one commit group), following the repo's rules:
idempotent command, `--force` to replace, drop-and-re-ingest for schema
changes, tests against the synthetic fixtures, and validation on the pilot
night against the salad numbers above.

### 1. Ingest the full survey's results (pilot, then full)

No new ingest logic is needed (`ingest results` from #13). New work:
- `results_ingest` provenance table (D1): run_id, source, ingest time, `shutter_offset_seconds` (0.5 for salad).
- **Scale**: 22.5M rows through SQLAlchemy `insertmanyvalues`. The
  20190402 run took ~43 s for 460k rows, which projects to roughly an
  hour for the survey, to be measured, not assumed. It is resumable
  because each run_id commits on its own.
- Run in a Slurm job (not a login node), with one process: `salloc -c 2
  --mem 8G`. The ingest reads salad's Postgres over localhost, which needs
  that server up on the same node. Only the five find-asteroids tables are
  read, with `yield_per` streaming.
- The non-standard run_id `deep-B1b-ccd20-20190827` has no
  `search_definition` match. It is ingested and reported as unmatched, as
  `ingest results` already does.
- Validation: per-table row counts equal salad's (5,573 / 276,797 /
  22,454,995 / 12,051,228 / 276,797), and every standard run_id matches a
  `search_definition`.
- Pilot first: the 60 runs for 20190402, already done in #14.

### 2. Crossmatch (`deep-asteroids crossmatch`)

```
deep-asteroids crossmatch [--night N ...] [--source S ...] [--max-sep-arcsec 30] [--force]
```

One table, `crossmatch_candidate`: one row per `(result, known_tracklet)`
pair whose mean separation is below the cutoff. Columns: `result_id`,
`known_tracklet_id`, `source`, `n_times`, `mean`/`min`/`max`/`total`
separation (arcsec), the velocity difference, and `known_tracklet`'s
`night`. "Closest candidate per result and source" is a query/view over it.
It stores all candidates because the same result often matches both an
mpchecker object and a submitted tracklet, and salad's single-best row
lost that.

Algorithm, per night:
1. Take the night's `known_tracklet` rows that have a fit (flag NULL or
   `large_residual`) and its results, grouped by search (each has its own
   exposure times from `search_definition_exposure`).
2. For each search, predict the result tracklets and the known
   tracklets at that search's exposure midpoints (UTC to TAI, minus the
   D1's 0.5 s shutter offset for the result side) and take great-circle separations.
3. Avoid the all-pairs product: prefilter with a KD-tree on unit vectors
   at the search's mid-exposure, with radius = cutoff + the largest
   motion over the baseline, then compute the exact mean separation only
   for surviving pairs.
4. Write with one writer; compute in at most `$SLURM_CPUS_PER_TASK`
   workers (no `nproc`).

Tests: synthetic fixture with a result built exactly on a known tracklet
(match at ~0"), one 1" off, one far away, and a result on the wrong night;
the shutter offset (a result labelled 0.5 s late matches at ~0" only after
the offset is applied); idempotence/`--force`.

Validation on 20190402 against salad's best-match counts by source, with
the expected differences listed rather than hidden: no refit, no trksub
merge, and the corrected timestamps.

### 3. Redetection dedup (D2)

Port `dedup.py`/`result_merge.py` as one stage over a `(night, field)`
pointing group: cluster results whose tracklets agree on average over the
shared exposure grid (`cluster_fits`'s union-find, with the grid from
`search_definition_exposure`, so no DB round trip), and mark one result per
cluster authoritative. Open: whether to refit the authoritative tracklet
from the union of the cluster's de-duplicated points (salad does), or just
pick the member with the most points. Validate by cluster counts per
night against salad's 276,747 membership rows.

### 4. Summary and filters (`deep-asteroids query summary`)

Ported from `summarize_results.py`: a funnel table per night/survey:
- results, authoritative results,
- matched to submitted observations (< 2"),
- matched to synthetic (< 2"),
- matched to mpchecker (< 10"),
- the "putative new" subset: median residual of gathered points against
  the result's own tracklet (`result_statistics.py`, computed here from
  `gathered`/`tracklet`), with salad's cuts (log10 residual < -0.2,
  10 < n_gathered < 150, speed inside the search's velocity range), and no
  known match.

Needs a `result_statistics` table (one row per result: `median_residual_arcsec`,
`n_gathered`, `n_residual_outliers`), computed in one vectorized pass, not
per result in Python.

### 5. Plots (`deep-asteroids plot ...`)

Same groups as salad's analysis (known / mpchecker / unmatched in velocity
range / outside range):
- residual distributions by group (`plot_residual_by_group.py`),
- median-magnitude histograms by group. Magnitudes now come straight from
  `gathered.mag`, so the salad `photometry` join is not needed,
- synthetic-object completeness vs magnitude (`plot_fake_completeness_magnitude.py`),
  the one plot that measures the search itself,
- per-result sky plot and cutouts for any result id, reusing #14's
  `examples/viz_result.py` logic (move it into `viz/`).

Output PNGs under a directory the user names; nothing is written to the
shared filesystems unprompted.

## Compute and shared-system notes

- All ingest and crossmatch work runs inside a Slurm allocation, sized
  from `$SLURM_CPUS_PER_TASK`/`$SLURM_MEM_PER_NODE`; defaults to 1 worker.
- No scans of `/gscratch` trees; the only reads are salad's Postgres, deep-db
  and the ingested `deep_asteroids.sqlite`.
- Check `hyakstorage` for the target fileset before the full ingest (D3),
  and again after, since quota data lags ~15 minutes.

## Open questions

1. D1-D5 above.
2. The crossmatch cutoff of 30" for stored candidates: is the per-result
   candidate list long enough to matter for the DB size in dense fields?
   Measure on the pilot before the full run.
3. Whether the full-survey run should also bring over salad's own
   `result_dedup_membership` and `crossmatch` rows as a comparison
   baseline (a separate `--baseline` import, never mixed into ours).
