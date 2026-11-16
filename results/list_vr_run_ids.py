#!/usr/bin/env python
"""Enumerates the run_ids `search_backfill_vr.sh` (#28) submits: every ``band='VR'``
``search_definition`` whose ``(night, field, detector_number)`` already has a searched (has a
find-asteroids ``search`` row) ``band IS NULL`` search_definition -- "the same searches we've
ingested results for" -- restricted further to ones whose every exposure already has a
``differenceExp`` image ingested.

Also excludes any run_id that already has a `search` row (i.e. already searched) -- makes
re-running this script after a partial batch naturally return only what's left to do, which
`search_backfill_vr.sh`'s submitter relies on to stay under Slurm's MaxSubmitJobsPerUser
across repeated invocations (see its own header comment).

That second restriction (image completeness) matters because `search run` has no
partial-coverage tolerance:
`detect_exposures()` (search/detect.py) raises outright if even one of a search definition's
exposures lacks an image row, rather than skipping it. Checked against the real full-survey
database (2026-11): 5,572 search definitions are in scope by the first rule, but only 4,261
(76.5%) pass the second -- the other 1,311 have at least one exposure with no actual
`differenceExp` image in the registry, a real data gap (not every planned detector-exposure
was actually observed/calibrated for that night), confirmed directly against two of a 12-run
calibration sample that failed exactly this way. Submitting those anyway would just be 1,311
guaranteed-failing array tasks for no benefit.

Writes one run_id per line to stdout -- redirect to the file `search_backfill_vr.sh` reads
(``$RUN_ID_LIST``, default ``results/vr_run_ids.txt``)."""
from find_asteroids.models import Search
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from deep_asteroids.database import get_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition as SD
from deep_asteroids.database.search import SearchDefinitionExposure as SDE


def in_scope_vr_run_ids(session):
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
        if n_images >= n_exposures:
            run_ids.append(run_id)
    return run_ids


if __name__ == "__main__":
    with Session(get_db()) as session:
        for run_id in in_scope_vr_run_ids(session):
            print(run_id)
