# Results

Scripts that run against the full-survey database, not one validation night
or a handful of known objects (that's what `examples/` demos). Two things
live here: `full_survey_summary.sh` (#15), which builds that database and
its funnel/summary plots, and `package_backfill_putative_real.sh` (#32/#33),
which renders PDFs from it.

## Full-survey ingest and funnel (#15)

```bash
cd results
DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/$USER/deep_asteroids_full_survey.sqlite \
DEEP_DB=/path/to/cached/deep_database.sqlite \
./full_survey_summary.sh
```

From inside a Slurm job (not a login node): ingests and crossmatches the
whole survey (~277k results), real CPU work. `RESULTS_DB` (default salad's
local Postgres, `postgresql://$USER@localhost:55432/salad`) and `OUT_DIR`
(default `../docs/results`) can also be overridden. See the script's own
header comment for what each step does, and `docs/results/README.md` for
the funnel numbers and plot descriptions this run produced.

Idempotent: every `deep-asteroids` step it calls skips work already done
(`ingest`/`link`/`dedup`/`crossmatch`/`statistics` all check first), so
re-running after a partial failure only repeats what didn't finish.

## Batch-rendering putative-real PDFs (#32/#33)

```bash
cd results
DEEP_ASTEROIDS_DB=sqlite:////gscratch/<group>/$USER/deep_asteroids_full_survey.sqlite \
DEEP_DB=/path/to/cached/deep_database.sqlite \
source /gscratch/dirac/DEEP/data/credentials \
./package_backfill_putative_real.sh
```

From a login node (it only submits `sbatch` jobs, does no rendering itself):
submits one `sbatch` job per night that has at least one putative-real
result at `sigma_v < 0.00015` (`$SIGMA_V_THRESHOLD` overrides), each
rendering that night's results (`deep-asteroids package result
--putative-real`) across `$CORES` (default 16) workers on `-p ckpt-all -A
ckpt-astro` (`$PARTITION`/`$ACCOUNT` override -- ckpt is preemptible, cheap,
and this workload doesn't need a dedicated allocation), plus one final
`package index` job depending on all of them. See the script's own header
comment for the full option list, and PR #33 for the runtime/storage
estimate behind the defaults.

Also preemptible- and restart-safe: `package result`'s own
skip-if-`index.html`-exists check is the whole manifest (#32's "no new
tracking table"), so a requeued or resubmitted run only (re)does whatever
didn't finish.
