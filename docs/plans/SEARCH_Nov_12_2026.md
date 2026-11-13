# Plan for issue #16: search infrastructure, and reproducing a previous search

Issue #16: "create search + ingest infrastructure using find-asteroids;
check that searches from the last run are reproducible."

Plan only, for review before any code. Items marked **DECISION** need the
owner's call, each with a proposed default. Salad numbers come from its
live Postgres (`localhost:55432/salad`) on 2026-11-12.

Detect and search are plain primitives, in the style of `viz/tracklet.py`,
with **no task queue**. State is just files and rows: a catalog file per
image, and a `search` row per run_id. Both make a rerun idempotent.

## Pinning down the timing difference (done)

The suspected ~32 s offset in salad's times does not exist (details in #18).
Measured on night 20190402, 139 recovered synthetic objects, which are
injected at deep-db's midpoint and so are exact truth: the results'
tracklets, evaluated at deep-db's midpoint converted UTC to TAI, are a median
0.03" from the truth, an implied time offset of **-0.18 +/- 0.22 s**.

Where the time comes from, traced end to end:

| step | time |
|---|---|
| image header `DATE-AVG` (TAI) | `ODATEOBS` + exposure/2 + 37 s |
| deep-db `exposure.midpoint` (UTC) | `obstime + exposure_time/2` (`deep_db/ingest/survey.py:85`), no shutter term |
| salad detection engine, catalog `MJD` (`detect_common.h`) | VisitInfo date as UTC **+ 0.5 s** (DECam shutter latency): `58576.215349169` vs deep-db `58576.215343382` for visit 845580, a difference of exactly 0.500 s |
| find-asteroids, `normalize_time()` | reads the bare MJD as UTC, converts to TAI (+37 s) |
| ingested `gathered.time` | `58576.2157774102` = the catalog MJD + 37 s, exactly |

So the previous search's detections carry deep-db's midpoint **+ 0.5 s**,
and everything downstream is consistent. 0.5 s is ~0.005" for a 0.26 deg/day
mover, far below anything visible, but it matters for reproducing the
previous run **exactly**: to get the same catalog, the same 0.5 s must be
there. The earlier note about a ~69 s discrepancy was most likely about
salad's `image` table times (a separate, unused column), not the catalog.

**DECISION 1 (shutter label).** For the reproduction, apply the 0.5 s
(`--shutter-offset-seconds 0.5`) so the new run is comparable to the old
one. Our wrapper takes each exposure's time from deep-db
(`search_definition_exposure.midpoint_mjd`, UTC) plus that constant, rather
than from the engine's own VisitInfo-derived MJD, so there is one source of
truth, and the engine's MJD is only checked against it (they should agree
to a few microseconds). The constant is recorded with the run. Proposed
default for new searches after the reproduction: 0 s, since the 0.5 s is
not physical in deep-db's convention, to be confirmed against the DECam
shutter model. Not needed before the reproduction is done.

## What already exists

- `search_definition`/`search_definition_exposure`: the unit of one search,
  with each exposure's midpoint. `ingest searches` writes both a per-band
  row and a `band = NULL` row; for `A0b` ccd42 on 20190402 they have the
  same 103 exposures over the same span (58576.215343 to 58576.391119), so
  the `-VR` search is exactly comparable to salad's run.
- `image`: Butler path of every detector-exposure's `differenceExp`.
- `ingest results` (#13): reads a `run_search()` results directory into
  `search`/`result`/`gathered`/`points`/`tracklet` under a run_id. A search
  run only needs to write a results directory and call it.
- `util/cutout.py`: the pattern for wrapping a vendored C++ binary with no
  LSST/Python runtime dependency, which detection reuses.
- `viz/tracklet.py`: the pattern for the new modules (primitives, a thin CLI
  on top, a summary dataclass, no queue).

## What the previous run did

The target is salad's run `deep-decam-A0b-ccd42-20190402`: 103 images, 50
results, parameters `velocity=[0.1, 0.5]` deg/day, `angle=[0, 359.99]`,
`dx=10`, `num_results=50`, `refine_iterations=1`, no psfs file, CPU search
(no `gpu`/`gpu_kernels`/`precompute`), detection by `detect_and_measure`
at threshold 5.0, positive polarity (the defaults; the older task rows
record no detector parameters, which the reproduction should confirm,
below). Salad's recorded cost for that task: **36.9 s**.

Its detection catalogs were deleted by `/gscratch/scrubbed`'s 21-day purge
(0 files remain), so detection has to be rerun as part of the reproduction.
That is also a feature: the old `gathered` rows are a subset of the old
catalog (ra, dec, time, flux, mag), so they can be matched against the new
catalog detection by detection, which checks the detector independently of
the search.

## Step 1. The search infrastructure

New code, following `viz/tracklet.py`'s shape (modules of primitives, then a
thin CLI), and nothing that touches a queue or a scheduler:

| piece | what | reference |
|---|---|---|
| `util/detect_engine/` | vendor salad's real `detect_common.h`, `detect_and_measure.cc`, `detect_csv_to_table.py` byte-for-byte, plus an adapted `build.sh` and a `VENDORED_FROM.md`, like `cutout_engine/` (whose notes say to vendor the full engine separately when detection is needed) | `salad/src/salad/detect/engine/` |
| `util/detect.py` | `LSSTDetector.detect(image_uri, threshold=5.0, polarity="positive") -> astropy Table`, wrapping the binary, same shape as `util/cutout.py` | `salad/detect/lsst_cpp.py` |
| `search/detect.py` | `detect_exposures(session, search_definition, catalog_dir)`: for each exposure's image, write `<catalog_dir>/<detector_exposure_id>.ecsv` with a provenance header (engine, threshold, polarity, image uri, shutter offset); skips a file that exists, `force` redoes | `salad/detect/__init__.py` (`write_catalog`) |
| `search/catalog.py` | `build_catalog(session, search_definition, catalog_dir) -> Table`: every exposure's catalog stacked in time order with `ra`/`dec` in degrees, `time` as a UTC `Time` (so find-asteroids converts it), plus `flux`/`mag` and a `detector_exposure_id` column. Handles an empty exposure (no rows) | `salad/search.py::catalog_for_images` and its empty-catalog fixes |
| `search/run.py` | `run_search_for_definition(session, search_definition, catalog, velocity, angle, dx, num_results, refine_iterations, seed)`: calls `find_asteroids.search.run_search` into a temporary results directory, then `ingest_results_dir(..., run_id=search_definition.run_id, params=...)`. Skips a run_id that already has a `search` row unless `force` | `salad/search.py::run_search_for_images` |
| CLI | `deep-asteroids search detect` and `deep-asteroids search run`, selecting by `--night/--field/--detector-number/--band` (or `--run-id`), with the search parameters as options and salad's as defaults | `cli/ingest.py` |

Design choices:
- **Catalogs are files, one per image,** not database rows: there are far too
  many peaks. They are cheap to regenerate (detection is deterministic), so
  the location is a choice, not a risk. **DECISION 2:** `--catalog-dir`
  required, no default, never written to a shared filesystem unprompted. For
  the pilot, a directory under the user's `/gscratch` space. Before the full
  survey, per-image files (436k of them) should be packed per
  `(night, field, detector)`, as the cutout work learned; deferred.
- **Determinism.** The Hough search is deterministic on CPU, and the
  refinement stage is not quite: `mcd_regression` builds
  `sklearn.covariance.MinCovDet` with no `random_state`, so which points it
  calls outliers can vary run to run. The wrapper seeds numpy's global RNG
  (`--seed`, default fixed, recorded in the run) before the refinement, so
  our own reruns are bit-reproducible. It cannot reproduce the old run's
  unseeded RNG state, which is why the comparison below has tolerances for
  that stage and exactness for the earlier ones. **DECISION 3:** seed in our
  wrapper now (proposed), and separately propose upstream that find-asteroids
  take a `random_state`; that upstream change is out of this issue.
- **Sizing from the allocation.** Thread counts from `$SLURM_CPUS_PER_TASK`
  (`NUMBA_NUM_THREADS`, `OMP_NUM_THREADS`); one detection subprocess per
  allocated CPU at most; no `nproc`. Defaults to 1.
- **CPU only.** Salad's run was CPU, and a GPU run would change the
  numerics we are trying to compare. GPU stays an option for later.
- **Versions.** The previous run used find-asteroids `0.1.4.dev5` and numba
  0.67.0; this repo pins `bcbab55` (`0.1.4.dev7`) with numba 0.68.1. The
  only `src/` change since the Sept 3 normalization commit is the empty-
  lattice error (Sept 26), which does not touch the algorithm, so we expect
  no difference. Recorded in the comparison report rather than assumed.
- **Tests:** a synthetic fixture (a few tiny catalogs with a planted mover
  across several exposures) through `build_catalog` and
  `run_search_for_definition`, asserting the planted mover is result 0 with
  the right velocity; idempotence and `force`; an empty exposure. The C++
  detector gets a test that skips cleanly when the binary is not built, as
  the cutout engine's does.

## Step 2. Rerun one search

`band = 'VR'`, night 20190402, field A0b, detector 42: the search
definition `deep-decam-A0b-ccd42-20190402-VR` (103 exposures).

```
deep-asteroids search detect --night 20190402 --field A0b --detector-number 42 --band VR --catalog-dir DIR
deep-asteroids search run    --night 20190402 --field A0b --detector-number 42 --band VR --catalog-dir DIR \
    --velocity 0.1 0.5 --angle 0 359.99 --dx 10 --num-results 50 --refine-iterations 1
```

Before relying on the new engine build, a one-image check first, as for the
cutout engine: build `detect_and_measure` against `opt_lsst` (the same
`w_2023_38` as for the cutout engine) and diff its CSV against salad's
already-built copy on one image. They should be byte-identical, which
settles whether the build, and not the code, can move numbers.

## Step 3. Compare with the previous run

A script (`examples/compare_search.py`, with a shell wrapper like the other
examples) compares the new run `deep-decam-A0b-ccd42-20190402-VR` to the old
`deep-decam-A0b-ccd42-20190402` already ingested (both live in the same
database, joined by `run_id`), at four levels, from the most to the least
deterministic, reporting counts instead of one pass/fail:

| level | check | expected |
|---|---|---|
| L0 detector | every distinct old `gathered` point matches a new catalog row in the same exposure within 0.05"; `flux`/`mag` agree; per-image detection counts | all match (the engine is deterministic, and the label is shared via Decision 1) |
| L1 Hough search | per `result_num`: `x, y, direction, n` | identical for all 50, in the same order (the CPU search is deterministic on the same catalog; **catalog row order matters** because ties in the Hough maximum may break by position, so we stack in the old image-id order) |
| L2 refinement | `tracklet` parameters, `n1/n2/n5/n10`, and the gathered set per result | agree within the noise floor: first rerun the new search twice, seeded and unseeded, to measure it, then require old-vs-new differences within a small multiple of that floor |
| L3 outputs | the same through `gathered`, `points`, the ingested rows | consistent with L2 |

The first level that fails says where the difference enters (detector,
catalog assembly, Hough, refinement), which is why the order matters. If
L0 fails, check the engine build and the time label first; if L1 fails,
check the catalog order and versions.

## Time and cost

One CCD, estimated from salad's recorded run times (per-task wall times,
claimed to finished) and to be **measured** in the pilot:

| stage | estimate | basis |
|---|---|---|
| build the detect engine, one time | minutes | the cutout engine's build |
| detect, 103 images | ~6 min (3.27 s mean per image) | salad's mean over 436,313 image tasks (median 2.3 s, p90 4.2 s) |
| search | ~0.5 min (20.8 s mean; this task 36.9 s) | salad's mean over 5,573 search tasks (median 18.7 s, p90 33.7 s) |
| ingest, compare, rerun twice for the noise floor | ~1-2 min | `ingest results`: ~45 s for 60 runs |
| **total, 4 CPUs / 8 GB (this allocation)** | **~10-15 min wall** | memory to be measured with `/usr/bin/time -v` |

For scale only (not part of this issue): the whole survey is 436,313
detections at 3.27 s = **396.7 CPU-hours** plus 5,573 searches at 20.8 s =
**32.2 CPU-hours**, about **430 CPU-hours** in all. That is ~4.3 hours of
wall time on 100 cores, and it is detection that costs, not search (92%).
Because each image and each search is independently restartable, it fits
the preemptible `ckpt` partition (`--account ckpt-astro --partition
ckpt-all`); catalog storage still has to be measured on the pilot's 103
files.

## Open questions

1. D1-D3 above.
2. Salad's timings are per-task wall time on shared nodes, with no CPU count
   recorded, so the CPU-hour figures are rough; the pilot gives real
   per-image and per-search numbers on a known allocation.
3. Whether the old detection ran with `--no-masks` or other engine options
   not in the task's recorded parameters; L0 will show it if so.
4. Whether to keep the previous run's `search` row beside the new one
   (proposed, as the comparison needs both) once the full survey is ingested.
