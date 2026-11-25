# Search documentation

Documents covering deep-asteroids' search tooling and the deeper-search exploration from
issue #43:

- [`deeper_search_20190402.md`](deeper_search_20190402.md) -- what we found running the SNR>=3
  deeper search on night 20190402 across four parameterizations (mask-plane fix, `--num-results`,
  `--dx`), restricted to the field (A0b) that actually got a long-stare sequence, plus an
  independent cross-check against the `find-asteroids` algorithm paper (Stetzler et al. 2025) on
  its own dataset.
- [`compute_cost.md`](compute_cost.md) -- measured per-detector detect/search runtime under each
  parameterization, and the extrapolated compute cost of applying each to the whole survey.
- [`field_coverage.md`](field_coverage.md) -- every (night, field) pair in deep-db, ordered by
  exposure count, for picking which pointings are dense enough ("long-stare") to search usefully.

See also `docs/database.md` (database/Postgres setup) and `docs/results/README.md` (the
full-survey, default-threshold results).
