# Deeper search results: night 20190402 (#43)

Issue #43 asked whether lowering the detection threshold below the production default (SNR>=5)
can usefully recover fainter moving objects. This explores that on a single night, 20190402,
across four parameterizations plus the production baseline, and cross-checks the result against
the `find-asteroids` algorithm paper's own published numbers.

## The data: one field got a long-stare sequence, the others didn't

Night 20190402 has three fields in its patch:

| field | exposures | time span |
|---|---:|---|
| **A0b** | **103** | 58576.215 - 58576.391 MJD (~4.2 h) |
| A0a | 7 | 58576.203 - 58576.397 MJD |
| A0c | 6 | 58576.210 - 58576.402 MJD |

Only A0b got the night's long-stare sequence; A0a and A0c were visited only a handful of times
(brief calibration/pointing checks, not a search-depth sequence). A0a/A0c still pass
`list_night_run_ids.py`'s >=2-exposure coverage filter, so they get searched along with A0b, but
with only 6-7 epochs there's essentially nothing to find there -- their synthetic/known objects
mostly just add unrecoverable noise to a *night-wide* completeness number. **All completeness
numbers below are restricted to A0b's 60 searched detectors** for a clean comparison; see
`README.md`'s own #43 section for the (noisier) night-wide numbers.

## One real artifact found and fixed along the way

A deeper threshold (SNR>=3) turned up a CR-interpolated bad column: ~530 of one tracklet's own
"detections" sat at the same 3-pixel-wide x, ~58% of them with `CR`/`INTRP` in their footprint --
neither plane is in the detection engine's default `--exclude-mask-planes` (matched to the
reference pipeline's own default). Fixed with two additive changes:

- `--exclude-mask-planes` is now configurable end-to-end (`$EXCLUDE_MASK_PLANES` in both
  `examples/night_summary.sh` and `examples/search_night_array.sh`).
- Every detection's own footprint-level mask union is now carried into the catalog as a `mask`
  column, propagated through to `gathered.extra` for later filtering/auditing (not just suppressed
  at detection time).

A per-detection mask breakdown of one affected result (every gathered point's mask, broken out by
LSST mask plane) confirmed no further mask bits bleed through once `CR`/`INTRP` are excluded --
remaining noise at SNR>=3 is genuine unmasked pixel noise, not a further maskable artifact.

## The four parameterizations (plus baseline), A0b only

| config | threshold | dx | num-results | synthetic recovered /269 findable | synthetic m50 | mpchecker recovered /374 findable |
|---|---:|---:|---:|---:|---:|---:|
| baseline (production) | 5 | 10 | 50 | 138 | 24.4 | 291 |
| step 1: mask fix (CR/INTRP excluded) | 3 | 10 | 50 | -- (full-night only; database since reused for step 2) | -- | -- |
| step 2: `--num-results 200` | 3 | 10 | 200 | 164 | 24.9 | 278 |
| step 3: `--dx 5` | 3 | 5 | 200 | **189** | **25.2** | **296** |
| step 4: `--num-results 1000` | 3 | 10 | 1000 | 175 | 25.0 | 283 |

(`m50`: the magnitude at 50% completeness, same logistic model as Stetzler et al. 2025 Eq. 73.
Each row's full-night, un-restricted numbers and funnel/putative-new counts are in `README.md`.)

Completeness plots (A0b only): `docs/examples/viz/night_20190402_snr_5_A0b/`,
`night_20190402_snr_3_A0b_step2_dx10_nr200/`, `night_20190402_snr_3_A0b_step3_dx5_nr200/`,
`night_20190402_snr_3_A0b_step4_dx10_nr1000/`.

## Findings

1. **Every deeper-search variant improves synthetic-object completeness over the SNR>=5
   baseline** (138 -> 164-189 of 269), as expected -- a lower threshold alone does recover more
   real objects, once the resulting noise is given somewhere to go (a wider mask exclude list, a
   finer grid, or a deeper ranked list).
2. **Halving `dx` (step 3) is a much stronger lever than raising `--num-results` 5x (step 4).**
   `dx=5` gets more depth (189 vs. 175 synthetic recovered; m50 25.2 vs. 25.0) from the *same*
   200-result budget step 2 used, while step 4's 1000-result budget produces far more noise
   (full-night putative-new candidates: 1,986 at step 2 -> 16,261 at step 4, vs. 5,979 at step 3)
   for a smaller completeness gain. The coarse `dx=10` grid, not ranking depth, was the real
   bottleneck at this threshold.
3. **mpchecker (known-object) completeness is a cautionary signal.** Steps 2 and 4 actually
   *underperform* the SNR>=5 baseline (278, 283 vs. 291 of 374) -- the flood of SNR 3-5 noise at a
   coarse `dx` crowds some real, already-known objects out of the ranked results entirely. Only
   step 3 (`dx=5`) beats the baseline there too (296). Lowering the SNR threshold is not a free
   improvement on its own; it needs a matching grid resolution to actually pay off, otherwise it
   can cost you objects you were already finding.

See `compute_cost.md` for what each of these costs in CPU time, and whether the gain is worth it
at survey scale.

## Cross-check against Stetzler et al. 2025

[Stetzler et al. 2025](https://arxiv.org/abs/2509.26279), "An Efficient Shift-and-Stack Algorithm
Applied to Detection Catalogs" -- the paper behind the `find-asteroids` package this repo's search
is built on -- validates its own algorithm on a single DEEP night/field: night 3 of the survey
(April 3, 2019 = night 20190403), field A0c, 104 VR exposures, 60 of 62 detectors searched (2
excluded for data-quality reasons).

We ran our own pipeline on that **exact same dataset**, with its MBA configuration: velocity range
0.1-0.5 deg/day (identical to our CLI's own default), SNR>=3, `dx=10` (their Table 1 for MBA at
SNR=3), 200 results/detector (their own fixed-output-budget convention for the Fig. 8/Table 2
SNR-vs-depth comparison).

**Result: m50 = 24.6, vs. the paper's reported MBA SNR>=3 depth of 24.84 +/- 0.06 (their Table
2).**

Completeness plot: `docs/examples/viz/paper_comparison_a0c_20190403/completeness_synthetic.png`.

A ~0.24 mag difference using an independent run through our own pipeline (not the paper's original
code) is a reasonable match, given known differences: LSP `w_2023_38` here vs. `w_2024_09` in the
paper; our 2" match radius vs. their <1"-over->=50%-of-epochs criterion; independently-ingested
catalogs and deep-db cache snapshot. The completeness curve's shape (a sharp dropoff right around
mag 24-25, a ~100% plateau for bright objects) matches the paper's own Fig. 6/8 well. This is a
useful independent sanity check that `deep-asteroids`' reimplementation of the search tracks the
reference `find-asteroids` paper's own validated results.

## Reproducing

```bash
# night-wide search (see examples/search_night_array.sh's own docstring for all variables)
export DEEP_ASTEROIDS_DB=postgresql://$USER@<host>:55432/deep_asteroids_snr_3
export DEEP_DB=/path/to/cached/deep_database.sqlite
NIGHT=20190402 THRESHOLD=3 DX=10 NUM_RESULTS=200 \
EXCLUDE_MASK_PLANES=EDGE,SAT,SUSPECT,BAD,NO_DATA,STREAK,CROSSTALK,NOT_DEBLENDED,CR,INTRP \
./examples/search_night_array.sh

# restrict completeness plots to one field's run_ids
deep-asteroids plot completeness --run-id deep-decam-A0b-ccd1-20190402-VR --run-id ... --out completeness_synthetic.png
```
