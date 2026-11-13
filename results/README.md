# Results

Scripts that generate `docs/results/`: the full-survey funnel and summary
plots for #15. Unlike `examples/` (demos against one validation night or a
handful of known objects), this is specifically the full-survey pipeline run.

## Running

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
