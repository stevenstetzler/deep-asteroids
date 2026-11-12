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
    [--out-dir DIR] [--product-type P] [--cutout-size N]
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

Verified against issue #5's three published test cases, using the real
Butler registry/repo and vendored engine end to end:

| case | identity | result |
|---|---|---|
| mpchecker ephemeris | `permid=612388` (2002 PV170), night 20220825 | 92/93 exposures measured; coadd shows a clean, centered point source |
| MPC observation | `permid=592187` (2014 QZ79), night 20201019, trkid `00000FFqBy` | 88/88 exposures measured; a second trkid that night for the same object (`00000FFq8F`) is correctly skipped (flagged `same_obstime_conflict`, no fit) |
| synthetic binary | `orbit_id=1009536` ('tno'), night 20211005 | all four components (primary/companion/barycenter/photocenter) measured at 103/103 exposures each; each component's coadd is a clean, centered detection (the binary's two injected components resolve as a single, slightly elongated blend in the primary/companion coadds, consistent with their known sub-pixel separation) |

Example `light_curve.png`/`coadd.png` pairs for all three cases are in
`docs/examples/viz/`.

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
