# Examples

Reproduce the three `viz tracklet` examples from issue #5 / PR #12
(`docs/examples/viz/`) end to end, against the real published deep-db,
the real Butler registry, and the vendored `cutout_and_photometry`
engine.

## Prerequisites

Run from an interactive or batch Slurm job (`salloc -c 2 --mem 8G`), not
a login node -- these do real ingest work and run the cutout engine.

1. Install deep-asteroids (`pip install -e '.[dev]'` from the repo root)
   and build the vendored cutout engine (see
   `src/deep_asteroids/util/cutout_engine/VENDORED_FROM.md`).
2. Fetch deep-db once: `deep-asteroids database fetch-deep-db`. `$DEEP_DB`
   can also point at an existing copy -- see the main README's
   "Databases" section.
3. Set the Butler registry credentials: `source
   /gscratch/dirac/DEEP/data/credentials` (sets `$PGUSER`/`$PGPASSWORD`).

## Running

```bash
cd examples
./mpchecker_612388.sh
./mpc_observation_592187.sh
./synthetic_503133.sh
```

Each script is self-contained and idempotent (re-running skips ingest
work already done) and writes its own `viz/<case>/` output directory,
plus a `deep_asteroids_examples.sqlite` database in the current
directory (override with `$DEEP_ASTEROIDS_DB`; override the output
directory with `$OUT_DIR`). `$REPO_DIR`/`$REGISTRY_HOST` default to the
real klone paths and can be overridden too -- see `common.sh`. Each
`movie.gif` is annotated per frame with `$ANNOTATE` (default
`{night}/{field}/{expnum}`, in white at the top of the frame) -- see
`deep-asteroids viz tracklet --help`/the main README for the fields
available.

## What each one does

| script | object | night / field | why |
|---|---|---|---|
| `mpchecker_612388.sh` | mpchecker ephemeris, 2002 PV170 (permid 612388) | 20220825 / B1e | 92/93 predictions on-chip |
| `mpc_observation_592187.sh` | MPC observation, 2014 QZ79 (permid 592187), trkid `00000FFqBy` | 20201019 / B1d | 88/88 observations on-chip; a second trkid for the same object that night is flagged `same_obstime_conflict` and intentionally not used here (see the script's own comment) |
| `synthetic_503133.sh` | synthetic TNO binary, orbit_id 503133 (4.0" separation) | 20190708 / B0a | 121/121 on-chip for both components; wide enough to see barycenter vs. photocenter differ by eye |

Each writes `light_curve.png`, `coadd.png`, `movie.gif` and one `<id>.fits`
cutout per exposure -- see the main README's "Visualizing known objects"
section for what each one shows.
