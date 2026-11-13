# Full-survey results (#15)

Summary plots and the funnel table for deep-asteroids' own ingest of the
full survey's find-asteroids results (5,573 runs, copied from salad's
database), crossmatched against submitted MPC observations, mpchecker
ephemerides and synthetic objects. Regenerate with `results/full_survey_summary.sh`
(see `results/README.md`).

## Funnel

| group | objects | putative real | fraction |
|---|---:|---:|---:|
| submitted | 35,298 | 33,868 | 95.9% |
| synthetic | 11,838 | 9,170 | 77.5% |
| known (mpchecker) | 4,487 | 4,035 | 89.9% |
| unmatched (in velocity range) | 106,113 | 19,245 | 18.1% |
| unmatched (outside velocity range) | 107,949 | 0 | 0.0% |
| **total distinct objects** | **265,685** | **66,318** | **25.0%** |

276,797 results in total, de-duplicated to 265,685 distinct objects.
**Putative new** (putative real and matching nothing): 19,245. The
putative-real cut is now **sigma_v** (the fitted tracklet's velocity
uncertainty, `hypot(sigma_vra, sigma_vdec)`) below `0.00025` deg/day, and
speed strictly inside the search's velocity range -- it keeps 96% of
submitted objects, 77% of synthetic, 90% of known.

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
  of any feature tried (see salad's `docs/ANALYZE_RESULT_STATS.md`).
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
  objects vs. magnitude (24,845 findable, 9,022 recovered overall).

Produced from `/gscratch/dirac/stevengs/claude/deep_asteroids_full_survey.sqlite`
via `deep-asteroids plot sigma-v/residuals/magnitudes/completeness` and
`deep-asteroids summary` (defaults: 2" submitted/synthetic radius, 10"
known radius, sigma_v < 0.00025 deg/day, authoritative objects only).
