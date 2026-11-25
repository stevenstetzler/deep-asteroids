# Where the databases live

deep-asteroids talks to two databases (see the top-level README's "Databases" section for the
full description of what each holds). This page is just their concrete locations on klone, for this
project's own working copies.

## deep-asteroids (read/write)

Search definitions, known-object tracklets, and find-asteroids' own `search`/`result`/`tracklet`/
`gathered`/`points` tables. Default (`$DEEP_ASTEROIDS_DB` unset): `./deep_asteroids.sqlite` in the
current directory -- a throwaway, created on first use.

**Production (#47): Postgres**, on the cluster already running for salad/tracklet:

| | |
|---|---|
| Server | `postgres -D /mmfs1/gscratch/dirac/stevengs/claude/salad/pgdata`, port 55432, already running |
| Database | `deep_asteroids` |
| URL | `postgresql://$USER@localhost:55432/deep_asteroids` |
| Auth | trust (loopback + the cluster's internal subnets; no password) |

```bash
export PATH=/mmfs1/gscratch/dirac/shared/opt/conda/envs/postgres/bin:$PATH   # psql/createdb/pg_ctl/...
export LD_LIBRARY_PATH="$(pg_config --libdir):$LD_LIBRARY_PATH"             # psycopg2 needs libpq.so.5
export DEEP_ASTEROIDS_DB=postgresql://$USER@localhost:55432/deep_asteroids
```

**`localhost` only works from n3064 itself.** From any other node (e.g. a Slurm job array's tasks,
which can land anywhere -- see `examples/search_night_array.sh`), use n3064's real hostname instead:
`postgresql://$USER@n3064.hyak.local:55432/deep_asteroids` (`getent hosts n3064` on n3064 itself
gives this). `pg_hba.conf` already trusts the cluster's internal subnets, so no password is needed
either way -- only the hostname needs to change.

Migrated from the full-survey SQLite file with `results/migrate_to_postgres.py` (73.6M rows, 16
tables, ~29 minutes); see that script's docstring and `results/README.md`'s "Transition to Postgres"
section.

**Legacy SQLite copies** (pre-migration; kept as historical/offline snapshots, not written to
anymore):

| File | Size | Notes |
|---|---:|---|
| `/mmfs1/gscratch/dirac/stevengs/claude/deep_asteroids_full_survey.sqlite` | 6.9 GB | the live working copy before #47; has `-wal`/`-shm` siblings (WAL mode) |
| `/mmfs1/gscratch/dirac/stevengs/claude/deep_asteroids_full_survey_ro.sqlite` | 6.8 GB | a `VACUUM INTO` snapshot of the above, opened `mode=ro&immutable=1` -- safe to read from any node without WAL's same-host requirement |
| `/mmfs1/gscratch/dirac/stevengs/claude/deep_asteroids_20190402.sqlite` | 43 MB | one night only, for quick local checks |

**Tests** default to a fresh temporary SQLite file per test (no setup needed). `pytest
--db-backend=postgres`/`both` additionally (or instead) runs them against a scratch
`deep_asteroids_test` database on the same cluster, created on first use. See the README's "Postgres"
section and `tests/conftest.py`.

## deep-db (read-only)

Survey geometry, MPC observations, mpchecker ephemerides, synthetic objects. Never downloaded
implicitly -- fetch once with `deep-asteroids database fetch-deep-db`, which caches it at
`$XDG_CACHE_HOME/deep_db/deep_database.sqlite` (default location; empty in this project's workspace,
since `$DEEP_DB` is set explicitly instead -- see below).

| | |
|---|---|
| File | `/mmfs1/gscratch/dirac/stevengs/claude/deep_database.sqlite` |
| Size | 1.7 GB |
| URL | `sqlite:////mmfs1/gscratch/dirac/stevengs/claude/deep_database.sqlite` (four slashes: absolute path), or just the plain path in `$DEEP_DB` |

```bash
export DEEP_DB=/mmfs1/gscratch/dirac/stevengs/claude/deep_database.sqlite
```

Always opened read-only and immutable, regardless of which form `$DEEP_DB` takes.
