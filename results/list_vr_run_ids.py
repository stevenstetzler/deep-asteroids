#!/usr/bin/env python
"""Enumerates the run_ids `search_backfill_vr.sh` (#28) submits: every ``band='VR'``
``search_definition`` whose ``(night, field, detector_number)`` already has a searched (has a
find-asteroids ``search`` row) ``band IS NULL`` search_definition -- "the same searches we've
ingested results for" -- restricted further to ones with enough ``differenceExp`` images ingested:
at least ``--min-coverage`` of its exposures (default 0) and never fewer than two.

Also excludes any run_id that already has a `search` row (i.e. already searched) -- makes
re-running this script after a partial batch naturally return only what's left to do, which
`search_backfill_vr.sh`'s submitter relies on to stay under Slurm's MaxSubmitJobsPerUser
across repeated invocations (see its own header comment).

That second restriction used to be "every exposure has an image", because `detect_exposures()`
(search/detect.py) raised if even one exposure lacked an image row. Checked against the real
full-survey database (2026-11), 5,572 search definitions are in scope by the first rule, but only
4,261 had every image: the other 1,311 lack at least one `differenceExp` that the Butler registry
does not have under any run (the one exception, night 20190401's `diff_drp` collection, is ingested
with `ingest images --fallback-collection`). Salad searched 1,244 of those on the images it had, so
`search run` now does the same -- it skips an exposure with no image, records it in
`search_missing_image`, and `--min-coverage` sets how much of a search must be imaged.

Writes one run_id per line to stdout -- redirect to the file `search_backfill_vr.sh` reads
(``$RUN_ID_LIST``, default ``results/vr_run_ids.txt``)."""
import argparse
import math

from find_asteroids.models import Search
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from deep_asteroids.database import get_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition as SD
from deep_asteroids.database.search import SearchDefinitionExposure as SDE


MIN_EXPOSURES = 2  # search.detect.MIN_EXPOSURES


def in_scope_vr_run_ids(session, min_coverage=0.0):
    base = aliased(SD)
    in_scope = session.execute(
        select(SD.id, SD.run_id, SD.n_exposures)
        .join(base, (base.night == SD.night) & (base.field == SD.field)
              & (base.detector_number == SD.detector_number) & (base.band.is_(None)))
        .join(Search, Search.run_id == base.run_id)
        .where(SD.band == "VR")
    ).all()
    already_searched = set(session.scalars(
        select(Search.run_id).where(Search.run_id.in_([r.run_id for r in in_scope]))
    ))

    run_ids = []
    for sd_id, run_id, n_exposures in in_scope:
        if run_id in already_searched:
            continue  # already has a `search` row -- `search run` would just skip it anyway
        n_images = session.scalar(
            select(func.count(func.distinct(SDE.deep_db_detector_exposure_id)))
            .select_from(SDE).join(
                Image, (Image.deep_db_detector_exposure_id == SDE.deep_db_detector_exposure_id)
                & (Image.product_type == "differenceExp"))
            .where(SDE.search_definition_id == sd_id)
        )
        if n_images >= max(MIN_EXPOSURES, math.ceil(min_coverage * n_exposures)):
            run_ids.append(run_id)
    return run_ids


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--min-coverage", type=float, default=0.0,
                        help="fraction of a search's exposures that must have an image (default 0)")
    args = parser.parse_args()
    with Session(get_db()) as session:
        for run_id in in_scope_vr_run_ids(session, args.min_coverage):
            print(run_id)
