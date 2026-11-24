#!/usr/bin/env python
"""Enumerates the run_ids `search_night_array.sh` (#43) submits: every `search_definition` of one
night and band with at least `--min-coverage` of its exposures imaged (`differenceExp`, never fewer
than two -- `search.detect.MIN_EXPOSURES`), excluding any that already have a `search` row. Mirrors
`results/list_vr_run_ids.py`'s own image-coverage check (see its docstring for why: `search run`
raises instead of searching when an exposure has no image, so submitting one of those to the array
would just waste a task).

Writes one run_id per line to stdout.
"""
import argparse
import math

from find_asteroids.models import Search
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from deep_asteroids.database import get_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition as SD
from deep_asteroids.database.search import SearchDefinitionExposure as SDE
from deep_asteroids.database.search import band_clause, parse_band

MIN_EXPOSURES = 2  # search.detect.MIN_EXPOSURES


def run_ids_to_search(session, night, band=Ellipsis, min_coverage=0.0):
    rows = session.execute(
        select(SD.id, SD.run_id, SD.n_exposures).where(SD.night == night, band_clause(band))
    ).all()
    already_searched = set(session.scalars(
        select(Search.run_id).where(Search.run_id.in_([r.run_id for r in rows]))))

    run_ids = []
    for sd_id, run_id, n_exposures in rows:
        if run_id in already_searched:
            continue
        n_images = session.scalar(
            select(func.count(func.distinct(SDE.deep_db_detector_exposure_id)))
            .select_from(SDE).join(
                Image, (Image.deep_db_detector_exposure_id == SDE.deep_db_detector_exposure_id)
                & (Image.product_type == "differenceExp"))
            .where(SDE.search_definition_id == sd_id)
        )
        if n_images >= max(MIN_EXPOSURES, math.ceil(min_coverage * n_exposures)):
            run_ids.append(run_id)
    return sorted(run_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--night", type=int, required=True)
    parser.add_argument("--band", type=str, default="VR",
                        help="a band, or '' / none for the undefined-band (all-band) rows (default VR)")
    parser.add_argument("--min-coverage", type=float, default=0.0,
                        help="fraction of a search's exposures that must have an image (default 0)")
    args = parser.parse_args()
    with Session(get_db()) as session:
        for run_id in run_ids_to_search(session, args.night, parse_band(args.band), args.min_coverage):
            print(run_id)
