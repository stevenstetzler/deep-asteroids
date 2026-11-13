# Full-survey results (#15)

Summary plots and the funnel table for deep-asteroids' own ingest of the
full survey's find-asteroids results (5,573 runs, copied from salad's
database), crossmatched against submitted MPC observations, mpchecker
ephemerides and synthetic objects.

## Funnel

| group | objects | putative real | fraction |
|---|---:|---:|---:|
| submitted | 35,298 | 33,622 | 95.3% |
| synthetic | 11,838 | 9,076 | 76.7% |
| known (mpchecker) | 4,487 | 3,883 | 86.5% |
| unmatched (in velocity range) | 106,113 | 18,587 | 17.5% |
| unmatched (outside velocity range) | 107,949 | 0 | 0.0% |
| **total distinct objects** | **265,685** | **65,168** | **24.5%** |

276,797 results in total, de-duplicated to 265,685 distinct objects.
**Putative new** (putative real and matching nothing): 18,587. The
putative-real cut (median residual < 0.63", 10–150 gathered points,
speed strictly inside the search's velocity range) keeps 95% of
submitted objects, 77% of synthetic, 87% of known -- in line with
salad's own validation on night 20190402.

## Plots

- **`magnitudes.png`** -- histogram of each result's median magnitude,
  by match group (submitted / synthetic / known / unmatched).
- **`residuals.png`** -- histogram of log10(median residual) by match
  group, with the putative-real cut marked. Clearly bimodal: a tight
  peak below the cut for real moving objects (submitted, synthetic,
  known), a broad one above it for chance alignments (unmatched).
- **`completeness.png`** -- recovered fraction of findable synthetic
  objects vs. magnitude (24,845 findable, 9,022 recovered overall).

Produced from `/gscratch/dirac/stevengs/claude/deep_asteroids_full_survey.sqlite`
via `deep-asteroids plot magnitudes/residuals/completeness` and
`deep-asteroids summary` (defaults: 2" submitted/synthetic radius, 10"
known radius, authoritative objects only).
