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
from find_asteroids.models import Search
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.crossmatch.classify import Thresholds, classify, load_results
from deep_asteroids.database import get_db
from deep_asteroids.database.search import SearchDefinition, band_clause, parse_band


def putative_real_run_ids(session, sigma_v_threshold=0.00015, band="VR"):
    """Run_ids of ``band``'s searches (a band as ``load_results`` takes it) with a putative-real result."""
    q = select(SearchDefinition.run_id).distinct()
    if (clause := band_clause(band)) is not None:
        q = q.where(clause)
    defined = {r for r, in session.execute(q)}  # a Search.run_id with no definition cannot be packaged
    run_ids = [r for r, in session.execute(select(Search.run_id).distinct()) if r in defined]
    rows = load_results(session, run_ids=run_ids, band=band)
    classify(rows, Thresholds(sigma_v_threshold=sigma_v_threshold))
    return sorted({r.run_id for r in rows if r.putative_real})


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
