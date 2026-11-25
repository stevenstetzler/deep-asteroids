# Compute cost of the deeper search (#43)

Per-detector detect/search runtime measured directly from each config's `search_run` rows
(`detect_seconds`/`search_seconds`, wall-clock), and an extrapolation of what running each config
across the whole survey would cost. See `deeper_search_20190402.md` for what each config actually
bought in completeness.

## Single-detector runtime

Median wall-clock, A0b/20190402's 60 detectors (103 exposures each -- same underlying images
across all five rows, so this isolates the effect of the search parameters themselves):

| config | detect (s) | search (s) | total (s) |
|---|---:|---:|---:|
| baseline (SNR>=5, dx=10, nr=50) | 58.4 | 10.7 | 69.1 |
| step 1: mask fix (SNR>=3, dx=10, nr=50) | 77.1 | ~12.7* | ~89.8 |
| step 2: `--num-results 200` | 77.1 | 29.5 | 106.6 |
| step 3: `--dx 5` | 77.1 | 87.1 | 164.2 |
| step 4: `--num-results 1000` | 77.1 | 118.9 | 196.0 |

\* Step 1's own database was dropped (before step 2's full re-run reused the same
`deep_asteroids_snr_3` name) before this analysis, so its search time wasn't preserved. The value
shown is interpolated from the num-results=200 -> num-results=1000 search-time scaling (a fixed
clustering cost plus ~0.11 s/extra result), not a direct measurement.

Detect time is a property of `(threshold, exclude-mask-planes)` only, not `dx`/`num-results` --
steps 1-4 all detect at threshold=3 with `CR,INTRP` excluded, so they share one set of catalogs on
disk (`CATALOG_DIR` is keyed by exposure id, not search parameters) and only the *first* of them to
run actually pays the 77.1 s detect cost; the rest hit a cache (confirmed: steps 3 and 4's own
`detect_seconds` are ~0, since their catalogs were already on disk from step 2's run).

Detect roughly doubles going from SNR>=5 to SNR>=3 (more peaks per image to process -- consistent
with Stetzler et al. 2025's own Fig. 4, which shows peak count rising sharply as the SNR threshold
drops). Search time is driven almost entirely by `dx` (grid density, scaling as `dx^-2` per the
paper's own Eq. 71) and `num-results` (per-candidate refinement cost, roughly linear): `dx=5` costs
~3x step 2's search time for the same `num-results`.

## Whole-survey extrapolation

Reference scale: the production `deep_asteroids` database's own extent -- **94 distinct (field,
night) long-stare pointings already searched, 5,572 total detector-searches** (~59.3
detectors/pointing, consistent with the nominal 60 CCDs/pointing used throughout). Each search task
uses 4 CPUs (`search_night_array.sh`'s default `$CORES`).

| config | core-hr / pointing (60 ccds) | core-hr, whole survey (5,572 detectors) | wall-clock, whole survey @ throttle=32 |
|---|---:|---:|---:|
| baseline | 4.6 | 428 | 3.3 h |
| step 1 (mask fix) | 6.0 | 556 | 4.3 h |
| step 2 (nr=200) | 7.1 | 660 | 5.2 h |
| step 3 (dx=5) | 10.9 | 1,016 | 7.9 h |
| step 4 (nr=1000) | 13.1 | 1,214 | 9.5 h |

(Wall-clock assumes full use of the array's `$THROTTLE=32` concurrent tasks, i.e. total task-seconds
/ 32; actual wall-clock depends on how much of the `compute-bigmem-astro` allocation is free at
submit time -- see the shared-system caveats in `search_night_array.sh`'s own docstring.)

**Takeaway:** going from the production baseline to the best-performing variant found so far (step
3, `dx=5`) costs **~2.4x the core-hours** (428 -> 1,016 core-hr) to re-search the survey-to-date --
a few hundred extra core-hours, under 8 wall-clock hours at the current array throttle. Step 4
(`--num-results 1000`) costs slightly *more* (1,214 core-hr) for a *smaller* completeness gain than
step 3 (see `deeper_search_20190402.md`'s findings), so `dx` is the more compute-efficient lever of
the two explored here: it buys more depth per core-hour than brute-forcing a deeper ranked-results
list.

## Caveats

- These are single-night, single-field (A0b, 103 exposures) measurements extrapolated linearly to
  5,572 detector-searches of varying exposure counts (`field_coverage.md` shows the real spread,
  from 1 to 185 exposures per pointing) -- a pointing with far fewer exposures will detect/search
  faster than this estimate, and one with more will be slower. Treat the whole-survey numbers as an
  order-of-magnitude estimate, not a precise forecast.
- Step 1's search time is interpolated, not measured (see above).
- `n_cpus=4` per task is this repo's own default, not a hard requirement; a different `$CORES`
  changes the core-hour total but not the wall-clock-per-task numbers measured here (search/detect
  use `$CORES` for an internal worker pool, not a 1:1 speedup).
