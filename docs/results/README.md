# Full-survey results (#15)

Summary plots and the funnel table for deep-asteroids' own ingest of the
full survey's find-asteroids results (5,573 runs, copied from salad's
database), crossmatched against submitted MPC observations, mpchecker
ephemerides and synthetic objects. Regenerate with `results/full_survey_summary.sh`
(see `results/README.md`).

## Funnel

Computed at the default radii (2" for submitted, synthetic and known) over the
original (non-`-VR`), salad-ingested runs: the 5,568 runs that have results
(their 276,797 results). The `-VR` re-executions (#28) are not used here; the
survey will move over to them in a later step.

| group | objects | putative real | fraction |
|---|---:|---:|---:|
| submitted | 35,297 | 33,868 | 96.0% |
| synthetic | 11,831 | 9,170 | 77.5% |
| known (mpchecker) | 4,188 | 3,885 | 92.8% |
| unmatched (in velocity range) | 106,372 | 19,395 | 18.2% |
| unmatched (outside velocity range) | 107,527 | 0 | 0.0% |
| **total distinct objects** | **265,215** | **66,318** | **25.0%** |

276,797 results in total, de-duplicated to 265,215 distinct objects.
**Putative new** (putative real and matching nothing): 19,395. The
putative-real cut is **sigma_v** (the fitted tracklet's velocity
uncertainty, `hypot(sigma_vra, sigma_vdec)`) below `0.00025` deg/day, and
speed strictly inside the search's velocity range -- it keeps 96% of
submitted objects, 78% of synthetic, 93% of known.

**The known radius is now 2", not 10"** (#42): at 10" a track that merely
crosses an ephemeris, and diverges from it, could match. Against the same
runs at 10", only the known-dependent rows change: known 4,485 -> 4,188,
unmatched in range 106,111 -> 106,372 and putative new 19,245 -> 19,395. The
submitted and synthetic rows and the 66,318 putative real in total are
unchanged.

**Distinct objects.** This page used to report 265,685 distinct objects
(and 107,949 unmatched outside the velocity range); a fresh run over the same
runs gives 265,215 (and 107,491), with identical putative-real counts. De-duplication now
clusters the `-VR` re-executions together with the original results, and a
cluster is counted only if its authoritative result is in one of the runs
used here. Clusters whose authoritative result is a `-VR` one are dropped even
when an original result is a member, so the distinct-object counts here
run about 470 (0.2%) below the original-only value. The result and
putative-real counts are not affected.

This replaces the earlier median-residual/`n_gathered` cut: salad's own
further analysis (`docs/ANALYZE_RESULT_STATS.md` in the salad repo) found
`sigma_v` the cleanest separator of real moving objects from chance
alignments -- clean separation at every contour level tried, where
`n_gathered` showed none at all and median residual, while usable, was
noisier. Median residual is still computed and plotted (`residuals.png`)
as a diagnostic; it is no longer the cut.

## Plots

- **`sigma_v.png`** -- histogram of log10(sigma_v) by match group, with
  the putative-real cut marked. Clearly bimodal: a tight peak below the
  cut for real moving objects (submitted, synthetic, known), a broad one
  above it for chance alignments (unmatched) -- the cleanest separation
  of any feature tried (see salad's `docs/ANALYZE_RESULT_STATS.md`). Drawn
  at `sigma_v < 0.00015`, the stricter cut `results/package_backfill_putative_real.sh`
  renders PDFs against (#32/#33) -- stricter than the funnel table above
  (still `0.00025`, unchanged), so this plot's cut line sits left of
  where it would at the funnel's own threshold.
- **`residuals.png`** -- histogram of log10(median residual) by match
  group. A diagnostic, not the cut (see above) -- the two populations
  happen to separate at a similar point on this axis too, just less
  cleanly than on `sigma_v`.
- **`magnitudes.png`** -- three panels of each result's median
  magnitude, by match group: raw counts (log scale, top), a linear-scale
  stacked total excluding `unmatched, not putative real` (bottom-most by
  group but overwhelming by count, so excluded to keep the smaller
  groups' shapes visible; middle), and each group's own fraction
  (bottom).
- **`completeness.png`** -- recovered fraction of findable synthetic
  objects vs. magnitude (24,845 findable, 9,022 recovered overall; unchanged
  by the 2" known radius, which does not affect the synthetic radius).

Produced from `/gscratch/dirac/stevengs/claude/deep_asteroids_full_survey.sqlite`
via `deep-asteroids plot sigma-v/residuals/magnitudes/completeness` and
`deep-asteroids summary` (defaults: 2" submitted/synthetic radius, 2"
known radius, sigma_v < 0.00025 deg/day, authoritative objects only).
