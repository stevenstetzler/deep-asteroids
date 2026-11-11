# deep-asteroids

Find asteroids in DEEP.

`deep-asteroids` runs [find-asteroids](https://github.com/stevenstetzler/find-asteroids)
shift-and-stack searches over the DECam Ecliptic Exploration Project
(DEEP), using [deep-db](https://github.com/stevenstetzler/deep-db) as its
source of survey geometry, MPC observations, mpchecker ephemerides and
synthetic (fake) objects.

> Status: phase 0. The package skeleton, the CLI and the database layer
> exist. Ingesting search definitions and known-object tracklets from
> deep-db comes next.

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
- `ingest` and `query` are placeholders for phase 1.

## Acknowledgements

Portions of the code in this repository have been built using AI tools,
including Claude.
