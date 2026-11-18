#!/usr/bin/env python
"""Enumerates the run_ids `package_backfill_putative_real_by_run.sh` (#38) submits: every
run_id (excluding VR ones, since none of the VR batch's own results have `statistics`/
`crossmatch`/`dedup` run on them yet -- found while sizing this very backfill, see PR #39)
with at least one putative-real result, using the exact same classification `package result
--putative-real`/`query results --putative-real` already use.

Also excludes any ``Search.run_id`` with no matching ``search_definition.run_id`` -- a real,
pre-existing data quirk (one row found, 2026-11: ``deep-B1b-ccd20-20190827`` in the ``search``
table vs. ``deep-decam-B1b-ccd20-20190827`` in ``search_definition``, a naming mismatch from
however that run's results were originally ingested), which would otherwise make `package
result --run-id` fail outright with "no search definition matches" -- `resolve_search_
definitions()` looks up by `search_definition.run_id`, not `search.run_id`.

Checked against the real full-survey database (2026-11): 276,797 non-VR results across 5,573
run_ids, 66,606 of them putative real (same total #32/#33's original backfill rendered),
spread across 5,228 distinct run_ids (minus the one excluded above).

Writes one run_id per line to stdout, sorted -- redirect to the file
`package_backfill_putative_real_by_run.sh` reads (``$RUN_ID_LIST``, default
``results/putative_real_run_ids.txt``)."""
import click
from find_asteroids.models import Search
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.crossmatch.classify import Thresholds, classify, load_results
from deep_asteroids.database import get_db
from deep_asteroids.database.search import SearchDefinition


def putative_real_run_ids(session, sigma_v_threshold=0.00015):
    defined = {r for r, in session.execute(select(SearchDefinition.run_id).distinct())}
    run_ids = [r for r, in session.execute(select(Search.run_id).distinct())
              if not r.endswith("-VR") and r in defined]
    rows = load_results(session, run_ids=run_ids)
    classify(rows, Thresholds(sigma_v_threshold=sigma_v_threshold))
    return sorted({r.run_id for r in rows if r.putative_real})


@click.command()
@click.option("--sigma-v-threshold", type=float, default=0.00015, show_default=True)
def main(sigma_v_threshold):
    with Session(get_db()) as session:
        for run_id in putative_real_run_ids(session, sigma_v_threshold=sigma_v_threshold):
            click.echo(run_id)


if __name__ == "__main__":
    main()
