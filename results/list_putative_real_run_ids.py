#!/usr/bin/env python
"""Enumerates the run_ids `package_backfill_putative_real_by_run.sh` (#38) submits: every
run_id of one band (``--band``, default VR; ``none`` for the undefined-band legacy searches)
with at least one putative-real result, using the exact same classification `package result
--putative-real`/`query results --putative-real` already use. Needs `statistics`, `crossmatch`
and `dedup` run for that band (`dedup --band B --force`, once per band).

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
from find_asteroids.models import Result, Search
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.database import get_db
from deep_asteroids.database.search import SearchDefinition, band_clause, parse_band
from deep_asteroids.database.statistics import ResultStatistics


def putative_real_run_ids(session, sigma_v_threshold=0.00015, band="VR"):
    """Run_ids of ``band``'s searches (a band as ``load_results`` takes it) with a putative-real result: ``sigma_v``
    below the threshold and the speed strictly inside the search's velocity range, the cut
    ``crossmatch.classify.classify`` applies -- as one query, not by loading and classifying every result. A
    run_id with no search definition cannot be packaged and is left out."""
    q = (select(Search.run_id).distinct()
         .join(Result, Result.search_id == Search.id)
         .join(ResultStatistics, ResultStatistics.result_id == Result.id)
         .join(SearchDefinition, SearchDefinition.run_id == Search.run_id)
         .where(ResultStatistics.sigma_v < sigma_v_threshold,
                ResultStatistics.speed > Search.velocity_0, ResultStatistics.speed < Search.velocity_1))
    if (clause := band_clause(band)) is not None:
        q = q.where(clause)
    return sorted(r for r, in session.execute(q))


@click.command()
@click.option("--sigma-v-threshold", type=float, default=0.00015, show_default=True)
@click.option("--band", type=str, default="VR", show_default=True,
              help="Only this band's searches; '' / none for the undefined-band (legacy) ones")
def main(sigma_v_threshold, band):
    with Session(get_db()) as session:
        for run_id in putative_real_run_ids(session, sigma_v_threshold=sigma_v_threshold, band=parse_band(band)):
            click.echo(run_id)


if __name__ == "__main__":
    main()
