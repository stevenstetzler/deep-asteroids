# Full-survey results (#15, #28)

Summary plots and the funnel table for deep-asteroids' full-survey results of the **band='VR'** searches:
5,572 searches re-executed from the Butler `differenceExp` images (#28, `results/search_backfill_vr.sh`; 5,566 have
results), crossmatched against submitted MPC observations, mpchecker ephemerides and synthetic objects. The legacy
(undefined-band) searches, salad's 5,573 runs copied from its database, stay in the same database and are compared
with the VR ones below. Every stage is run per band (`--band`), so the two are never mixed. Regenerate with
`results/full_survey_summary.sh` (see `results/README.md`).

## Funnel

Computed at the default radii (2" for submitted, synthetic and known), over the VR searches:

| group | objects | putative real | fraction |
|---|---:|---:|---:|
| submitted | 35,299 | 33,869 | 95.9% |
| synthetic | 11,798 | 9,167 | 77.7% |
| known (mpchecker) | 4,189 | 3,886 | 92.8% |
| unmatched (in velocity range) | 106,187 | 19,380 | 18.3% |
| unmatched (outside velocity range) | 107,833 | 0 | 0.0% |
| **total distinct objects** | **265,306** | **66,302** | **25.0%** |

275,269 results in total, de-duplicated to 265,306 distinct objects.
**Putative new** (putative real and matching nothing): 19,380. The
putative-real cut is **sigma_v** (the fitted tracklet's velocity
uncertainty, `hypot(sigma_vra, sigma_vdec)`) below `0.00025` deg/day, and
speed strictly inside the search's velocity range -- it keeps 96% of
submitted objects, 78% of synthetic, 93% of known.

## Legacy vs VR

The same funnel for the legacy searches (`--band none`), next to the VR one:

| | legacy | VR | VR - legacy |
|---|---:|---:|---:|
| results | 276,797 | 275,269 | -1,528 |
| distinct objects | 265,685 | 265,306 | -379 |
| submitted | 35,298 | 35,299 | +1 |
| synthetic | 11,838 | 11,798 | -40 |
| known (mpchecker) | 4,189 | 4,189 | 0 |
| unmatched (in velocity range) | 106,374 | 106,187 | -187 |
| unmatched (outside velocity range) | 107,986 | 107,833 | -153 |
| putative real: total | 66,318 | 66,302 | -16 |
| putative real: submitted | 33,868 | 33,869 | +1 |
| putative real: synthetic | 9,170 | 9,167 | -3 |
| putative real: known | 3,885 | 3,886 | +1 |
| putative new | 19,395 | 19,380 | -15 |

The VR searches reproduce the legacy ones to well under 1% in every row. They search the same exposures
(`results/legacy_vs_vr_parity.py`: all 435,908 exposures salad searched, on the same image files, are searched
by VR), with the same parameters, and the detection stage agrees on 99.998% of results; the refinement stage
differs in a minority of results (PR #45), which is where the 0.55% fewer results and the small differences above
come from. The 1,250 searches that skipped an image (22,499 detector-exposures with no `differenceExp` in the
Butler registry) skipped the same ones salad did.

**The known radius is 2", not 10"** (#42): at 10" a track that merely crosses an ephemeris, and diverges from
it, could match. On the VR results at 10", only the known-dependent rows change: known 4,471 -> 4,189, unmatched
in range 105,941 -> 106,187 and putative new 19,238 -> 19,380.

**Distinct objects are counted per band.** `dedup` clusters a pointing group's results, and the legacy and VR
searches cover the same sky, so clustering them together merges the same object found by both and makes a
band's distinct-object count depend on which band's result won the cluster (it gave 265,215 for the legacy
searches instead of 265,685). `dedup --band B --force`, once per band, clusters each on its own: the legacy
result above, 265,685, is the number this page has always reported (the 50 results of one run with no search
definition count as one object each).

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
  objects vs. magnitude (24,843 findable, 9,020 recovered overall; the legacy
  searches give 24,845 and 9,022). A band is recovered only by its own results
  (`--band VR`), not by a legacy match.
- **`position_residuals.png`** -- histograms of dRA/dDec (observed -
  predicted, arcsec) for the 1,374,671 detections of the 20,030 numbered
  known objects (permid set) a result matched within 2" (#46): both are
  tightly centred on zero (median -0.016"/-0.031", std 0.145"/0.136"),
  confirming find-asteroids' detections, deep-db's exposure midpoints and
  the known-object fits all agree at the ~0.1-0.2" level.
- **`position_residuals_vs_mag.png`** -- the same per-detection dRA/dDec
  against the object's own median magnitude (over its detections within
  1"), shown as 50/80/95% density contours over the individual
  detections (faint, underneath), with each 0.5-mag bin's median and
  +-1/2/3 robust (MAD) sigma overlaid (bins under 20 detections
  dropped): the scatter widens steadily fainter than ~mag 18, as
  expected for shot-noise-limited centroiding, and stays essentially
  flat and tight (well under 0.25") brightward of it.
- **`timing_uncertainty.png`** -- median +- std of dRA (C0)/dDec (C1)
  against a timing offset applied to the known objects' own evaluation
  time, scanned over +-1s in 0.05s steps (`results/timing_uncertainty.py`):
  the std stays flat at ~0.13-0.15" across the whole range (timing is not
  the limiting term) and the median drifts only slightly (a few mas per
  0.1s, from the matched objects' own on-sky motion), with no sign of an
  offset at a particular timing value that would improve agreement.

Produced from `/gscratch/dirac/stevengs/claude/deep_asteroids_full_survey.sqlite`
via `deep-asteroids plot sigma-v/residuals/magnitudes/completeness --band VR` and
`deep-asteroids summary --band VR` (`--band none` for the legacy column; defaults: 2" submitted/synthetic/known
radius, sigma_v < 0.00025 deg/day, authoritative objects only), after `statistics`, `crossmatch` and
`dedup --force` were run once per band.

The two position/timing-uncertainty plots (#46) are produced by standalone scripts instead (see
"Positional and timing uncertainty against numbered known objects" below):

```bash
DEEP_ASTEROIDS_DB=sqlite:////gscratch/dirac/stevengs/claude/deep_asteroids_full_survey.sqlite python \
  results/astrometric_uncertainty.py --band VR --out-hist docs/results/position_residuals.png \
  --out-vs-mag docs/results/position_residuals_vs_mag.png
DEEP_ASTEROIDS_DB=sqlite:////gscratch/dirac/stevengs/claude/deep_asteroids_full_survey.sqlite python \
  results/timing_uncertainty.py --band VR --out-plot docs/results/timing_uncertainty.png
```

## Positional and timing uncertainty against numbered known objects (#46)

Two scripts, run from a compute node against the full-survey database (read-only; see each script's
docstring for its arguments):

- `results/astrometric_uncertainty.py` -- numbered objects (`permid` set) a result matched within
  2", compared against that result's own gathered detections within 1" of the object's predicted
  position, as dRA/dDec residuals (observed - predicted). Writes a per-detection CSV and the
  `position_residuals*.png` plots above. 1,374,671 detections of 20,030 numbered objects over the
  VR searches, residuals tightly centred on zero (median -0.016"/-0.031", std 0.145"/0.136").
  `--timing-offset-seconds` applies a one-off shift to the known tracklet's evaluation time before
  comparing, for a quick check; the next script scans a whole grid of them.
- `results/timing_uncertainty.py` -- the same comparison, redone at a grid of timing offsets
  (default +-1s, 0.05s steps) applied to the known tracklet's evaluation time, logging the median
  and standard deviation of the kept dRA/dDec at each one (`timing_uncertainty.csv`) and the
  `timing_uncertainty.png` plot above. The crossmatch pairs and their gathered detections are
  fetched once and reused across every grid point, so the 41-point default scan costs about one
  `astrometric_uncertainty.py` run (under a minute over the VR full-survey results), not 41 of them.
