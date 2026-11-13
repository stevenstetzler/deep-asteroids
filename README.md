# deep-asteroids

Find asteroids in DEEP.

`deep-asteroids` runs [find-asteroids](https://github.com/stevenstetzler/find-asteroids)
shift-and-stack searches over the DECam Ecliptic Exploration Project
(DEEP), using [deep-db](https://github.com/stevenstetzler/deep-db) as its
source of survey geometry, MPC observations, mpchecker ephemerides and
synthetic (fake) objects.

> Status: phase 1. Known-object tracklets, search definitions, and
> images (linking exposures to Butler dataset paths) are ingested; a
> vendored forced-photometry/cutout engine is in place; and known
> objects' cutouts/light curves/coadds/movies can be visualized directly
> from a known_tracklet row. Phase 1 is complete.

## Installation

```bash
python -m pip install git+https://github.com/stevenstetzler/deep-asteroids
```

For development:

```bash
git clone https://github.com/stevenstetzler/deep-asteroids
cd deep-asteroids
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

`find-asteroids` and `deep-db` are pinned to specific GitHub commits in
`pyproject.toml`. deep-db depends on `jorbit`, which pulls in JAX, so the
install is fairly large. On a shared cluster, run it inside a batch or
interactive job rather than on a login node. Also point
`XDG_CACHE_HOME`/`PIP_CACHE_DIR` somewhere with quota if your home
directory is small.

## Databases

deep-asteroids talks to two databases:

| | What | Default | Override |
|---|---|---|---|
| **deep-asteroids** (read/write) | search definitions, known-object tracklets, and find-asteroids' own `search`/`result`/`tracklet`/`gathered`/`points` tables (same metadata, so everything joins in plain SQL) | `./deep_asteroids.sqlite` | `DEEP_ASTEROIDS_DB`, any SQLAlchemy URL |
| **deep-db** (read-only) | survey geometry, MPC observations, mpchecker ephemerides, synthetic objects | deep-db's cached copy of the published SQLite file, `$XDG_CACHE_HOME/deep_db/deep_database.sqlite` (1.7 GB), opened read-only and immutable | `DEEP_DB`, any SQLAlchemy URL |

`DEEP_DB` can be a plain file path (`DEEP_DB=/path/to/deep_database.sqlite`)
or a SQLAlchemy URL. Any SQLite target must already exist, and is always
opened read-only and immutable. In a SQLite URL, `sqlite:///path` is
**relative** to the current directory; an absolute path needs four
slashes, `sqlite:////abs/path`. A mistyped path is reported along with the
path that was actually resolved, rather than SQLite silently creating an
empty database there.

deep-asteroids **never downloads deep-db implicitly**. Fetch it once,
explicitly, on a machine with good network (e.g. a cluster login node):

```bash
deep-asteroids database fetch-deep-db
```

This wraps `deep_db.database.download_db()`: a streamed, checksummed,
atomic download with a `.sha256` sidecar. Everything else opens the
cached file read-only, so many concurrent processes can read it without
racing on downloads or checksums.

**Schema policy (for now): drop and re-ingest.** There are no migrations.
After a schema change, recreate the database and re-run the ingest steps:

```bash
deep-asteroids database create --force
```

## CLI

```
$ deep-asteroids --help
Usage: deep-asteroids [OPTIONS] COMMAND [ARGS]...

Options:
  --log-level TEXT  Set the logging level (DEBUG, INFO, WARNING, ERROR,
                    CRITICAL)
  --version         Show the version and exit.
  --help            Show this message and exit.

Commands:
  database  Create and inspect the deep-asteroids database; fetch deep-db.
  ingest    Ingest search definitions and known-object tracklets from...
  query     Query the deep-asteroids database (phase 1).
```

- `deep-asteroids database create [--force]` creates every table (with
  `--force`, it deletes the database first).
- `deep-asteroids database info [--exact]` shows both database URLs, row
  counts for our tables, and the deep-db file's path, size, sha256 and
  row counts.
  - By default, deep-db's counts come from `max(rowid)`, which is instant
    and exact for the append-only published snapshot.
  - `--exact` runs `count(*)` instead, which takes about 1.5 minutes cold
    on a networked filesystem.
- `deep-asteroids database fetch-deep-db` downloads deep-db, or confirms
  the cached copy is up to date.
- `deep-asteroids ingest searches`/`ingest tracklets` and
  `deep-asteroids query searches`/`query tracklets`: see below.

## Search definitions

```bash
deep-asteroids ingest searches [--night N ...] [--force]
```

One or more `search_definition` rows per `(night, field, detector)`
triple deep-db knows about -- one CCD of one pointing group over one
night, the unit find-asteroids searches. This ingests **every** triple;
science-field/minimum-exposure filtering is deferred to the search stage.

**Band is part of the grouping, not just a descriptive column**: each
triple gets one row per band actually present among its exposures
(`band` set, scoped to just that band's exposures), plus always one more
row with `band = NULL` spanning *every* exposure of the triple regardless
of band. A single-band triple therefore gets two rows (the band, and the
duplicate `NULL` union); a triple that mixes bands gets one row per band
plus the `NULL` union. This way neither a later single-band search nor a
later "search everything together" needs runtime re-grouping -- both are
already materialized.

`run_id` matches salad's own `deep-decam-<field>-ccd<detector_number>-
<night>` convention for the `band = NULL` row (e.g.
`deep-decam-A0b-ccd4-20190402` -- not zero-padded; verified against
salad's real `search_task.run_id` rows); a per-band row (salad has no
equivalent) appends `-<band>`.

Each search's exposures (`search_definition_exposure`) keep the time
baseline (`midpoint_mjd`) and chip footprint corners, copied from deep-db,
for later image-selection/photometry box tests to read. Exposures with no
field (`exposure.field_id IS NULL`, 9 survey-wide) form no pointing group
and are skipped.

Re-running skips nights already ingested; `--force` replaces them. The
full ingest takes well under a minute against the published deep-db,
writing about 43,600 searches (from 21,638 triples, 310 of which mix
bands) across 1.1M exposure-detector rows (each exposure counted once
per search it belongs to).

```bash
deep-asteroids query searches [FIELDS ...] [--night N] [--field F] [--detector-number D] [--band B|''|none] [--header] [--output FILE]
```

`--band ''`/`--band none` (same convention as `query tracklets --flag
none`) selects the undefined-band (`band = NULL`) rows -- the ones
spanning every band of their triple. Same `|`-separated style as
`query tracklets`.

## Images

```bash
deep-asteroids ingest images --registry-uri URL --repo-dir DIR --product-type differenceExp [--night N ...] [--field F ...] [--force]
```

Links deep-db detector-exposures to Butler dataset paths, one row per
`(detector-exposure, product type)`: `--registry-uri` is the Butler
repo's registry database URL (e.g.
`postgresql://user:pass@epyc.astro.washington.edu/deep_klone_2`, with
read-only credentials at `/gscratch/dirac/DEEP/data/credentials`) and
`--repo-dir` is the repo root its paths are relative to (e.g.
`/gscratch/dirac/DEEP/repo`) -- joined together to make a directly-
openable URI.

**Always scoped per `(night, field)` pointing group**, one registry
query per group (from deep-db's own list, optionally narrowed by
`--night`/`--field`), not run as one global query across the whole
survey: the real registry has thousands of RUN collections from
repeated reprocessing (526,900 distinct `(visit, detector)` pairs for
`differenceExp` alone, across 16M+ tagged rows), so a duplicate
`(visit, detector)` pair is only resolved correctly within one pointing
group's own collection-chain precedence (`DEEP/<night>/<field>`'s
CHAINED collection) -- a global "lowest dataset.id" tie-break would
silently pick an arbitrary reprocessing run's image instead.

Verified against the real registry (`psql -h epyc.astro.washington.edu
deep_klone_2`): night `20190402`/field `A0b` gives exactly 60 searchable
CCDs (CCD 2 excluded, matching #7's validation target) across its 103
exposures; the full survey ingest takes about 90 s and writes ~472k
images across 349 pointing groups.

Needs `psycopg2-binary` (a direct dependency) for the Postgres registry;
no extra `LD_LIBRARY_PATH`/module setup required.

```bash
deep-asteroids query images [FIELDS ...] [--night N] [--field F] [--detector-number D] [--product-type P] [--header] [--output FILE]
```

Same `|`-separated style as `query searches`/`query tracklets`.

## find-asteroids results

```bash
deep-asteroids ingest results --results-dir DIR --run-id RUN_ID [--output-format ecsv] [--force]
deep-asteroids ingest results --results-db URL [--run-id RUN_ID ...] [--night N ...] [--force]
```

Ingests find-asteroids results into its own `search`/`result`/`gathered`/
`points`/`tracklet` tables (same metadata as ours, so everything joins in
plain SQL), from either:

- **A results directory on disk**: `DIR/<result_num>/{result,gathered,
  points,tracklet}.<format>` as written by `run_search()`. `--run-id` names
  the run; columns the tables lack (e.g. `image_id`) are kept in `extra`.
- **Another find-asteroids results database** (any SQLAlchemy URL, e.g.
  salad's Postgres): every run, or just `--run-id`/`--night` ones (a night
  matches run_ids ending `-<night>` or `-<night>-<band>`), copied with ids
  remapped. Only the five find-asteroids tables are read.

A run's `Search.run_id` equal to a `search_definition.run_id` is the join
back to its (night, field, detector); the summary warns about run_ids with
no matching definition (run `ingest searches` first). Re-running skips
run_ids already ingested; `--force` replaces them. Values are copied
verbatim. Salad's times are correct to within a fraction of a second: for the
139 recovered synthetic objects of night 20190402, results evaluated at
deep-db's midpoint (UTC to TAI) sit a median 0.03" from the synthetic truth,
an implied time offset of -0.18 +/- 0.22 s
(`examples/time_offset_synthetic.py`), so there is no multi-second offset to
correct.

Verified on night 20190402 from salad's database: 60 runs, 3,003 results,
293,136 gathered, 168,828 points and 3,003 tracklets, matching the source
row for row, in about 45 s; all 60 run_ids match a `search_definition`.

## Searches

```bash
deep-asteroids search run (--run-id R | --night N --field F --detector-number D --band B) \
    [--catalog-dir DIR] [--velocity 0.1 0.5] [--angle 0 359.99] [--dx 10] [--num-results 50] \
    [--refine-iterations 1] [--seed 0] [--workers W] [--force] [--remove-catalogs]
deep-asteroids search detect (same selection) [--catalog-dir DIR] [--threshold 5] [--polarity positive] [--force]
```

Runs find-asteroids over a search definition and ingests the results under its
`run_id`. No queue: detection writes one `<detector_exposure_id>.ecsv` catalog
per image into `--catalog-dir` (default `/gscratch/scrubbed/$USER/deep_asteroids/
catalogs`; a catalog that exists is skipped), `search run` stacks them in time
order, runs the search and calls `ingest results`. Re-running skips a run_id
that already has results; `--force` replaces it. `--remove-catalogs` deletes
exactly that search's own catalog files once its results are committed (a
skipped or failed run deletes nothing). A `search_run` row per run records the
seed, the library versions, the CPU count and what each stage
cost.

Detection needs the vendored `detect_and_measure` engine built once (see
`src/deep_asteroids/util/detect_engine/VENDORED_FROM.md`; about 1.5 minutes
inside a Slurm job). Parallelism is sized from the Slurm allocation, never
`nproc`: one detection process per allocated CPU, and numba's thread count.

- **Time labels.** Each exposure's time is deep-db's exposure midpoint (UTC;
  find-asteroids converts it to TAI), the same time the known-object and
  synthetic ephemerides are evaluated at, so a search's timing matches theirs
  by construction. The detection engine's own exposure time is not used.
- **Determinism.** The detector and the Hough search are deterministic. The
  refinement stage is not: `mcd_regression`'s `MinCovDet` takes no
  `random_state`, so `--seed` seeds numpy's global RNG around the search
  (restoring its previous state afterwards). The same seed repeats exactly.

### Reproducing salad's run

`examples/search_ccd41.sh` reruns band VR, night 20190402, field A0b,
detector 41 (103 exposures, 50 results) and compares it with salad's earlier
run, level by level (the full report is in a comment on PR #20):

| level | result |
|---|---|
| L0 detector | all 7,065 distinct points salad's search gathered are found in the new catalog **exactly** (0" apart, identical flux and mag); the time labels agree to 0.04 ms |
| L1 Hough | `(x, y, direction, n)` identical for all 50 results, in the same order |
| L2 refinement | 19/50 results' tracklets differ by more than 0.05" somewhere in the span (max 2.0"); rerunning with seed 1 or 2 differs from seed 0 in 16-17/50 (max 1.2"), the same size |
| L3 outputs | the Hough-stage `points` sets are identical for all 50; the `gathered` sets are identical for 32/50 (min Jaccard 0.96, median 1.0) |

So everything but the unseeded refinement is reproduced exactly, and the
differences that remain are the refinement's own seed-to-seed noise. Result 24
(the example in `result_ccd41_24.sh`) is identical in all of these: same
Hough cluster, same counts, the same 110 `gathered` points and 105 `points`,
and a tracklet agreeing to 1e-11 deg/day. Its regenerated figures match the
ones made from salad's database: the movie is byte-identical, and the three
plots differ only in their title (the run id now ends in `-VR`) and 16
anti-aliased pixels along the track in `sky.png`.

Measured cost of one CCD on 4 CPUs / 8 GB: detecting its 103 images took 23 s
with the images in the file cache and 74 s cold (about 2.9 CPU-seconds per
image cold), the search 10 s, the ingest 3 s, and the peak memory was 557 MB
(salad's recorded means: 3.3 s per image and 21 s per search).

## De-duplicating and cross-matching results

```bash
deep-asteroids dedup [--night N ...] [--field F ...] [--run-id R ...] [--merge-radius-arcsec 2] [--force]
deep-asteroids crossmatch [mpc-linked|mpc-observations|mpc-ephemeris|synthetic ...] [--night N ...] [--run-id R ...] \
    [--max-sep-arcsec 30] [--submitted-radius-arcsec 2] [--known-radius-arcsec 10] [--synthetic-radius-arcsec 2] [--force]
deep-asteroids query crossmatches [--source S] [--night N] [--run-id R] [--max-mean-sep-arcsec X] [--all] \
    [--authoritative-only] [--header] [--output FILE]
```

**`dedup`** groups redetections of the same object among a night's results.
A search commonly finds one object in several adjacent search cells, and on
several CCDs of a pointing group, each a separate `result`/`tracklet`. Per
`(night, field)` group (every search of every CCD of that field that night),
two results are redetections when their tracklets agree, *on average over the
group's exposure times* (deep-db's midpoints, in TAI), to within
`--merge-radius-arcsec`; a cluster is the transitive closure of that. One member of
each cluster is authoritative: the one with the most Hough-stage points
(`result.n`), the lowest id breaking ties. Nothing is deleted, refit or
created: the `result_dedup` table records each result's cluster, and counting
distinct objects is counting `is_authoritative` rows. `--run-id` clusters only
those runs' results.

**`crossmatch`** matches each result's tracklet against every fitted
`known_tracklet` of the same night, evaluating both at the search's exposure
times (deep-db's UTC midpoints, in TAI; no offset anywhere). The sources are
the submitted observations, matched as **linked** tracklets (`mpc_linked`, one
per object's night, from `deep-asteroids link` below: the pieces of an object
split across trkids are merged and refit, so run `link` first; the default sources
are `mpc-linked`, `mpc-ephemeris` and `synthetic`, and a night that has MPC tracklets
but none linked is an error, not a silently empty source), the known objects
(`mpchecker` ephemerides) and the synthetic (fake) objects. The per-trkid
`mpc-observations` can still be matched explicitly. Every pair below
`--max-sep-arcsec` mean separation is stored in `crossmatch`, with its mean,
minimum, maximum and total separation and the velocity difference: all
candidates, not just the closest, since one result often matches both a
submitted tracklet and an ephemeris. The printed table counts the results
whose closest candidate of each source is within that source's radius
(submitted 2", known 10", synthetic 2"), and how many of them are distinct
(authoritative) objects. `query crossmatches` prints the closest candidate of
each source for each result (`--all` for every stored one).

Both are idempotent (`crossmatch` per search run, `dedup` per pointing group;
`--force` redoes) and need no deep-db: `known_tracklet` is already fit and
lives in the same database. Pairs are found without comparing all of them:
a pair whose mean separation is below a radius is below it at one of the
exposures, so a KD-tree per exposure finds every pair that comes near at any
of them, and only those are tested exactly (`crossmatch/geometry.py`; a test
checks this against brute force). Matching one 103-exposure CCD's 50 results
takes under a second; 61 searches of a night's 3,053 results against its
5,259 known tracklets take 15 s.

Verified on the seeded ccd41 search of night 20190402 (`examples/crossmatch_ccd41.sh`):

| | ours | salad's own, for the same 50 results |
|---|---|---|
| results with a candidate under 30" | 3 (results 26, 35, 43) | 3 (the same) |
| result 26 | submitted `00000F941o` 0.092", known 2003 UD129 0.063" | super_tracklet 0.054" |
| result 35 | synthetic orbit 3688014 0.031" | fake a3688014 0.033" |
| result 43 | submitted 2019 GC134 0.078", known 2019 GC134 0.123" | super_tracklet 0.119" |
| distinct objects (dedup) | 50 of 50 | 50 of 50 |

and, to exercise merging on real redetections, our run together with salad's
run of the same CCD finds every object twice: 100 results form 50 clusters of
exactly 2, each pairing the same `result_num`.

Over the whole night (60 searches, 3,003 results; each result classified by its
closest candidate, as salad stores): synthetic matches under 2" / 10" are 169 / 178
against salad's 169 / 177, and the total matched under 2" is 559 against 556. Which
of "submitted" and "known" a result is counted under when both match it (the closest
wins) differs (203 / 187 against salad's 258 / 129); that is a statement about which
of two fits is closer, not about what matches. Dedup
merges more than salad's did (3,003 results to 2,972 distinct objects against
salad's 2,997 within its runs): salad ran with an extra
`velocity_range: search` restriction and refit merged clusters, neither of which is
ported; with the same restriction we merge 1, not its 6, so the difference is not
fully explained.

Not done here (the plan's later steps): the per-result statistics and the "putative
new" filter, the summary funnel, and the plots.

## Linking MPC tracklets

```bash
deep-asteroids link [--night N ...] [--match-radius-arcsec 1] [--force]
```

deep-db's submissions sometimes split one object's night of observations across
several `trkid`s, and `ingest tracklets mpc-observations` fits each on its own.
`link` groups the fragments (the fitted ones, so not the `conflated_trkid`,
`same_obstime_conflict` or `too_few_points` rows) whose fits agree, *on average over the
night's exposures* (all of deep-db's that night, in TAI), to within
`--match-radius-arcsec` (1", salad's), by union-find (the same test `dedup`
uses; agreement at one exposure is not enough, since two unrelated tracks can
cross). A group of one keeps its fit; a larger group is refit over all its
members' observations, re-read from deep-db, with the same least-squares fit.
Each group is a `known_tracklet` row with source `mpc_linked` (the one crossmatching
matches "submitted" observations against by default; `mpc-observations` is still
selectable), with `n_members`, its combined `statuses`, and its `designations`. The
`known_tracklet_member` table ties it back to the per-trkid rows it was built from
(which carry `trkid`/`trksub`/designation); `query crossmatches` lists them as `members`.
A conflict is reported rather than resolved: a group joining
two different designations has `n_designations` 2 and no `provid`. Storing linked
rows (or replacing the per-trkid ones) invalidates the night's stored cross-matches,
which `crossmatch` then redoes.

Salad's `super_tracklet` (`mpc_link.py`) is the same idea; this differs in using the
exact pair search instead of HEALPix buckets and the repo's deterministic fit instead
of a robust randomized one. On night 20190402 it links the 1,017 tracklets into 824
groups (729 single, 78 pairs, 8 triples, and 9 larger groups of up to 17 trkids)
against salad's 820. On the 1,011 trkids both link, **810 of 820 groups are identical**;
the others differ at the margin (the pairs salad joins and we do not are 1.0 to 2.7"
apart on average, against our 1.0" radius), and the six trkids only we link are 3 or 4
observations over a few minutes.

```bash
deep-asteroids viz linked --night N --out-dir DIR [--min-members 2] [--linked-id ID ...] [--limit N] [--no-sheets]
```

`viz linked` draws one figure per merged tracklet, for checking the link: a RA/Dec
panel with each member's own observations (re-read from deep-db) and its own fit
(solid where observed, dashed across the night), under the merged fit; and a panel
with how far each member's fit is from the merged one at every exposure of the night,
each member's observed span shaded. It also tiles the figures into contact sheets.
Examples for night 20190402 are in `docs/examples/viz/linked_20190402/`. All 95
merged groups of that night pass two checks: every observation of every member lies
within 0.49" of its group's merged fit (the median group's median is 0.06"), and of
the 36 groups with designated members none joins two different designations (89
designated members joined consistently); the other 59 groups are undesignated, so the
geometry alone vouches for them.

What linking does and does not do for matching, measured on that night's 3,003
results (the same results matched against linked and against unlinked tracklets): the
**set of results that match a submitted tracklet is identical at every radius from 1" up**
(1", 2", 5", 10", 30"); below that it differs by one or two results (at 0.25", 2 matched only
linked and 1 only unlinked). The 79 results that match a merged group all also match one
of its fragments, about equally well (0.099" for the best fragment, 0.090" linked;
the linked one is closer in 38 of them). What it changes is the count of objects: the
330 matching results hit **531 distinct trkids but only 363 distinct objects**, and 78
results match two or more fragments of the same object, so counting submitted objects
without linking overcounts by 46%.

## Result statistics, the putative-real filter, and plots

```bash
deep-asteroids statistics [--night N ...] [--run-id R ...] [--force]
deep-asteroids summary [--night N ...] [--run-id R ...] [--all-results] [thresholds]
deep-asteroids query results [--group G] [--putative-real|--not-putative-real] [--putative-new] [--authoritative-only] \
    [--limit N] [--header] [--output FILE] [thresholds]
deep-asteroids plot residuals|sigma-v|magnitudes --out FILE [--night N ...] [--run-id R ...] [--all-results] [thresholds]
deep-asteroids plot completeness --out FILE [--source synthetic|mpchecker] [--min-exposure-fraction 0.5] \
    [--radius-arcsec X] [--bin-width 0.5] [--night N ...] [--run-id R ...]
```

**`statistics`** fills `result_statistics`, one row per result, from its own gathered points and tracklet, with
no cross-match: the **median residual** of its gathered points against its tracklet at each point's time (the
main separator of real moving objects, a few tenths of an arcsec, from chance alignments, arcsec-scale),
the number of points beyond 1" (`n_residual_outliers`), `n_gathered`, `n_times`, `speed` (`hypot(vra, vdec)`,
comparable to a search's velocity range), `sigma_v`, and the `median_mag` of the points. It is one query and one
vectorized pass per search run. On the 200 results of four runs where salad computed the same statistics
(salad's table is empty for night 20190402, whose searches predate it), `n_gathered`, the outlier count and the
median residual are **identical for all 200** (the residual bit-for-bit).

**Groups and the putative-real cut** (`crossmatch/classify.py`, salad's). Each result is in exactly one group:
`submitted` (a linked MPC tracklet within 2"), `synthetic` (within 2"; the closer wins if both),
`known` (an mpchecker ephemeris within 10"), else unmatched, split by whether its speed is inside its search's
velocity range. A result is **putative real** if its `sigma_v` (`hypot(sigma_vra, sigma_vdec)`, the fitted
tracklet's velocity uncertainty) is below `0.00025` deg/day and its speed is strictly inside the search's range;
**putative new** is putative real and unmatched. Every threshold is an option. Objects are counted as the
authoritative result of each `dedup` cluster, so redetections and duplicate searches count once.

`sigma_v` replaced an earlier median-residual/`n_gathered` cut after salad's further analysis
(`docs/ANALYZE_RESULT_STATS.md` in the salad repo) found it the cleanest separator of real moving objects from
chance alignments at every contour level tried, where `n_gathered` showed no separation at all and median
residual, while usable, was noisier. The median residual is still computed and plotted (`plot residuals`) as a
diagnostic; it is no longer the cut.

On night 20190402 (60 searches, 3,053 results = 2,972 distinct objects), the cut keeps **100% of the submitted
matches (329/329), 100% of the known (67/67) and 89% of the synthetic (140/158)**, the last because most of the
18 it rejects fail the velocity cut: synthetic objects outside the search's designed speed range. Of the 1,415
unmatched objects in range, **365 are putative new** (they peak at magnitude 23.5-24.5, faint, where objects not
yet submitted would be); the 1,003 unmatched outside the range fail the cut by definition, and 1,982 results
fail the `sigma_v` cut in all. `summary` prints this funnel; `query results` lists the rows.

**`plot`** writes PNGs (examples for the night in `docs/examples/viz/night_20190402/`): `sigma-v` (log10
sigma_v by group, with the cut marked -- the putative-real discriminant: matched objects sit in a tight low-
sigma_v peak, chance alignments in a broad high-sigma_v one), `residuals` (log10 median residual by group, a
diagnostic that separates similarly but less cleanly -- not the cut), `magnitudes` (each result's median
magnitude by group: raw counts, a linear-scale stacked total excluding the dominant `unmatched, not putative
real` group, and each group's own fraction), and `completeness`: the fraction of **findable** synthetic (or
mpchecker) objects that a result matched, against their
truth magnitude (`known_tracklet.mag`, the median of the night's points), with Wilson 68% intervals. An object is
findable only if its track is on a searched chip (tested geometrically against each exposure's chip footprint)
for at least half of that search's exposures *and* its speed is inside the search's range, so objects on chips,
fields or nights that were not searched are not counted as misses; it is recovered if a crossmatch candidate
(2" synthetic, 10" mpchecker) exists for it. For the night: **269 findable synthetic objects, 138 recovered,
~90-100% brighter than magnitude 24, 50% at 24.4, none fainter than 25**. As a check on the geometry, of the
158 synthetic objects some result matched, 138 are counted findable; of the other 20, 18 are outside the search's
speed range (so excluded by design, though a search sometimes finds them) and 2 cross a chip for under half the
exposures (for mpchecker, 297 of 300). The
mpchecker curve (374 findable, 297 recovered, 50% at 23.6) is less certain: its magnitude is a predicted V-band
`v_mag`, not the survey's band, and its ephemerides have their own uncertainty.

`examples/night_summary.sh` reproduces the whole night from scratch in about a minute (it needs salad's Postgres
for the results).

## Cutouts and forced photometry

`deep_asteroids.util.cutout.LSSTCutoutPhotometry` runs a vendored C++
binary, `cutout_and_photometry` (`util/cutout_engine/`), that reads a
real `differenceExp`/`calexp`'s pixels, PSF and photometric calibration
to do forced PSF photometry (flux/mag + uncertainty) and write a pixel
cutout at any caller-supplied sky position -- ported from salad's own
`cutout_and_photometry.cc` (see `util/cutout_engine/VENDORED_FROM.md`
for exactly what's vendored verbatim vs. trimmed, and why). Once built,
it has **no LSST/Python runtime dependency** -- `opt_lsst` is only
needed at build time:

```bash
cd src/deep_asteroids/util/cutout_engine
source /mmfs1/gscratch/dirac/shared/opt_lsst/bin/opt_lsst.sh && opt_lsst setup w_2023_38
bash build.sh
```

```python
from deep_asteroids.util.cutout import LSSTCutoutPhotometry, PhotometryRequest

engine = LSSTCutoutPhotometry()
for record in engine.measure(image_uri, [PhotometryRequest(id="1", ra=215.24, dec=-12.89)],
                              cutout_dir="cutouts", cutout_size=31):
    print(record)  # PhotometryRecord(in_bounds, flux, mag, ..., cutout_path)
```

Verified against a real DECam `differenceExp` (`DEEP/20190402/A0b`,
visit 845580, detector S25, from `ingest images`): forced photometry at
a real detected peak (`ix=1309, iy=89`) matches salad's own
`detect_and_measure`'s production forced-photometry output exactly
(flux 7095.26, mag 21.618, snr 19.66), and the freshly-built binary's
output is byte-for-byte identical to salad's own already-built copy at
the same position -- confirming the trimmed `detect_common.h` changed
nothing behavioral.

## Visualizing known objects

```bash
deep-asteroids viz tracklet --source {synthetic|mpc_observation|mpchecker} --night N \
    (--trkid T | --orbit-id O [--component C] | --provid P | --permid P) \
    [--out-dir DIR] [--product-type P] [--cutout-size N] [--annotate TEMPLATE]
```

For one `known_tracklet` row -- or every component of a synthetic
binary at once, if `--component` is omitted -- and every exposure its
own deep-db points touch on its night: runs forced photometry and
writes a pixel cutout at the object's **predicted** position (from the
tracklet's own linear fit, not the raw deep-db point -- this exercises
the fit itself), then renders `light_curve.png` (mag vs. MJD TAI),
`coadd.png` (median stack, ZScale-stretched) and `movie.gif` (one frame
per exposure, chronological, shared ZScale normalization, 4x
nearest-neighbor upscale, 2 fps). Needs `ingest images` and the vendored
cutout engine (above) already done. Default output directory:
`./viz/<source>/<night>/<identity>/`; resolving more than one row (a
binary) nests a subdirectory per component under an explicit `--out-dir`
too, so components never overwrite each other's plots or cutouts.

`--annotate` is a `str.format()` template (e.g. `'{night}/{field}/{expnum}'`)
drawn in white at the top of every `movie.gif` frame -- fields:
`night`, `field`, `expnum`, `mjd_tai`, `ra`, `dec`, `mag`. The font
shrinks (and, failing that, the text is truncated with an ellipsis) to
always fit within the frame's own width, however narrow `--cutout-size`
makes it.

Verified against issue #5's three published test cases, using the real
Butler registry/repo and vendored engine end to end:

| case | identity | result |
|---|---|---|
| mpchecker ephemeris | `permid=612388` (2002 PV170), night 20220825 | 92/93 exposures measured; coadd shows a clean, centered point source |
| MPC observation | `permid=592187` (2014 QZ79), night 20201019, trkid `00000FFqBy` | 88/88 exposures measured; a second trkid that night for the same object (`00000FFq8F`) is correctly skipped (flagged `same_obstime_conflict`, no fit) |
| synthetic binary | `orbit_id=503133` ('tno', 4.0" separation), night 20190708 | all four components (primary/companion/barycenter/photocenter) measured at 121/121 exposures each; primary/companion resolve as two separate detections, and the photocenter (flux-weighted) coadd sits visibly closer to the brighter primary than the barycenter (unweighted midpoint) does |

Example `light_curve.png`/`coadd.png`/`movie.gif` for all three cases are
in `docs/examples/viz/`; `examples/` has a self-contained, idempotent
script per case (`mpchecker_612388.sh`, `mpc_observation_592187.sh`,
`synthetic_503133.sh`) that reproduces them end to end against the real
registry/repo -- see `examples/README.md`.

## Known-object tracklets

```bash
deep-asteroids ingest tracklets [synthetic|mpc-observations|mpc-ephemeris ...] [--night N ...] [--force]
```

This fits one linear tracklet per known object per night from deep-db
into the `known_tracklet` table:

| source | from deep-db | one tracklet per |
|---|---|---|
| `synthetic` | `synthetic_object_ephemeris` | synthetic object × night. Each binary also gets a `barycenter` (mean of the two component positions) and a `photocenter` (flux-weighted by `delta_h`) tracklet |
| `mpc_observation` | `mpc_observation` | MPC tracklet (trkid/trksub) × night |
| `mpchecker` | `mpchecker_ephemeris` | MPC object × night, fit on every predicted point (`n_on_chip` counts those that landed on a detector) |

The model is `ra(t) = ra_epoch + vra·(t − epoch)` (and the same for
dec):
- Degrees and deg/day; `vra` is the plain dRA/dt, with no cos(dec) factor
  (find-asteroids' convention).
- Times are MJD **TAI**, with `epoch` at the mean time of the fitted points.
- `ra_0`/`dec_0` on the model give find-asteroids' MJD-0 convention.

Ephemerides are fit at the exposure midpoint; MPC observations use their
own `obstime`. Points aren't stored: each row keeps the deep-db ids
(`deep_db_synthetic_object_id`, `deep_db_mpc_tracklet_id`,
`deep_db_mpc_object_id`) to re-read them from, plus the `deep_db_sha` it
was built from.

Every deep-db object or tracklet on a night gets a row. `flag` explains
the ones without a usable fit:

| flag | fit | meaning |
|---|---|---|
| `too_few_points` | none | fewer than 2 distinct times that night |
| `conflated_trkid` | none | MPC tracklet whose observations are attributed to more than one object |
| `same_obstime_conflict` | none | MPC tracklet with two observations at the same time more than 5″ apart |
| `large_residual` | kept | a point more than `--max-residual-arcsec` (default 5″) from the line: mixed-up observations, or real curvature for a fast mover |

Re-running skips (source, night) pairs that are already ingested;
`--force` replaces them. The full ingest takes about 3.5 minutes and
1.5 GB of memory against the published deep-db, writing about 521k
tracklets.

```bash
deep-asteroids query tracklets [FIELDS ...] [--source S] [--night N] [--provid P] [--trkid T] \
    [--orbit-id O] [--component C] [--flag F|none] [--header] [--output FILE]
```

This prints rows separated by `|`. For example, all four tracklets of one
synthetic binary:

```bash
deep-asteroids query tracklets component night ra_epoch dec_epoch vra vdec --orbit-id 1002 --header
```

## Acknowledgements

Portions of the code in this repository have been built using AI tools,
including Claude.
