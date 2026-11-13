"""
Run find-asteroids over one search definition and ingest its results.

No queue: a search run is one function call. State is the per-image catalog
files (``search/detect.py``) and the ``search`` row for the run_id, so a rerun
skips what exists (``force`` replaces the run; catalogs are kept unless
``force``-detected). Ported from salad's ``search.py::run_search_for_images``
without its task tables.

The results land under the search definition's own ``run_id`` (so a run
joins ``search_definition`` and, for salad's runs, the previous results),
via ``ingest/results.py``'s ``ingest_results_dir``, with a ``search_run`` row
recording the seed, the time offset and what each stage cost.
"""
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import select

from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.database.search_run import SearchRun
from deep_asteroids.ingest.results import ingest_results_dir
from deep_asteroids.search.catalog import build_catalog
from deep_asteroids.search.detect import allocated_cpus, detect_exposures, remove_catalogs

log = logging.getLogger(__name__)

# salad's search parameters for the DEEP survey (see the migration plan)
DEFAULTS = {
    "velocity": (0.1, 0.5),  # deg/day
    "angle": (0.0, 359.99),  # deg
    "dx": 10.0,  # in units of the (unspecified) PSF scaling, so arcsec
    "num_results": 50,
    "refine_iterations": 1,
}
DEFAULT_SEED = 0


@dataclass
class SearchRunSummary:
    run_id: str
    ran: bool = False  # False: skipped, its run_id already has a search row
    n_exposures: int = 0
    n_detections: int = 0
    n_results: int = 0
    n_catalogs_written: int = 0
    n_catalogs_removed: int = 0
    max_time_discrepancy_seconds: float = 0.0
    detect_seconds: float = 0.0
    search_seconds: float = 0.0
    ingest_seconds: float = 0.0

    def format(self):
        if not self.ran:
            return f"{self.run_id}: skipped (already searched; --force to replace)"
        lines = [
            f"{self.run_id}: {self.n_results} result(s) from {self.n_detections:,} detection(s) "
            f"in {self.n_exposures} exposure(s)",
            f"  detect {self.detect_seconds:.1f} s ({self.n_catalogs_written} catalog(s) written), "
            f"search {self.search_seconds:.1f} s, ingest {self.ingest_seconds:.1f} s",
        ]
        if self.n_catalogs_removed:
            lines.append(f"  removed {self.n_catalogs_removed} catalog(s)")
        return "\n".join(lines)


def resolve_search_definitions(session, run_ids=None, night=None, field=None, detector_number=None,
                               band=Ellipsis):
    """``search_definition`` rows matching the given filters. ``band`` is
    ``Ellipsis`` for "any", ``None`` for the undefined-band rows, else a band."""
    q = select(SearchDefinition)
    if run_ids:
        q = q.where(SearchDefinition.run_id.in_(list(run_ids)))
    if night is not None:
        q = q.where(SearchDefinition.night == night)
    if field is not None:
        q = q.where(SearchDefinition.field == field)
    if detector_number is not None:
        q = q.where(SearchDefinition.detector_number == detector_number)
    if band is not Ellipsis:
        q = q.where(SearchDefinition.band.is_(None) if band is None else SearchDefinition.band == band)
    return session.scalars(q.order_by(SearchDefinition.run_id)).all()


def _versions():
    import find_asteroids
    import numba

    try:
        from importlib.metadata import version

        fa = version("find-asteroids")
    except Exception:  # noqa: BLE001
        fa = getattr(find_asteroids, "__version__", None)
    return fa, numba.__version__


def _seeded(seed, fn):
    """Run ``fn()`` with numpy's global RNG seeded, then restore the previous
    state: sklearn's ``MinCovDet`` (find-asteroids' refinement) draws from it
    and takes no ``random_state``, so this is what makes a rerun repeatable
    without affecting anything else in the process."""
    state = np.random.get_state()
    np.random.seed(seed)
    try:
        return fn()
    finally:
        np.random.set_state(state)


def run_search_for_definition(
    session, search_definition, catalog_dir, detector, velocity=DEFAULTS["velocity"],
    angle=DEFAULTS["angle"], dx=DEFAULTS["dx"], num_results=DEFAULTS["num_results"],
    refine_iterations=DEFAULTS["refine_iterations"], seed=DEFAULT_SEED, shutter_offset_seconds=0.0,
    workers=None, force=False, remove_catalogs_after=False,
):
    """Detect (any missing catalogs), search and ingest ``search_definition``
    under its ``run_id``. Returns a ``SearchRunSummary``. Commits.

    Skips a run_id that already has a ``search`` row unless ``force``, which
    deletes its rows first. With ``remove_catalogs_after``, deletes this
    search's catalog files (exactly those, see ``remove_catalogs``) once its
    results are committed; a skipped or failed run deletes nothing."""
    from find_asteroids.models import Result, Search
    from find_asteroids.search import run_search

    run_id = search_definition.run_id
    summary = SearchRunSummary(run_id=run_id, n_exposures=len(search_definition.exposures))
    existing = session.scalars(select(Search).where(Search.run_id == run_id)).first()
    if existing is not None and not force:
        return summary

    started = time.monotonic()
    detected = detect_exposures(session, search_definition, catalog_dir, detector, workers=workers, force=False)
    summary.detect_seconds = time.monotonic() - started
    summary.n_catalogs_written = detected.n_written

    catalog = build_catalog(search_definition, catalog_dir, shutter_offset_seconds=shutter_offset_seconds)
    summary.n_detections = len(catalog.table)
    summary.max_time_discrepancy_seconds = catalog.max_time_discrepancy_seconds

    import numba

    numba.set_num_threads(min(allocated_cpus(), numba.config.NUMBA_NUM_THREADS))
    with tempfile.TemporaryDirectory(prefix="deep-asteroids-search-") as tmpdir:
        tmpdir = Path(tmpdir)
        catalog_file = tmpdir / "catalog.ecsv"
        catalog.table.write(catalog_file, format="ascii.ecsv")
        results_dir = tmpdir / "results"

        started = time.monotonic()
        _seeded(seed, lambda: run_search(
            catalog=catalog_file, psfs=None, velocity=list(velocity), angle=list(angle), dx=dx,
            num_results=num_results, results_dir=results_dir, precompute=False, gpu=False,
            gpu_kernels=False, device=-1, output_format="ecsv", refine_iterations=refine_iterations,
        ))
        summary.search_seconds = time.monotonic() - started

        params = {
            "catalog": f"deep-asteroids:{run_id}", "psfs": None,
            "velocity_0": velocity[0], "velocity_1": velocity[1], "angle_0": angle[0], "angle_1": angle[1],
            "dx": dx, "num_results": num_results, "results_dir": None,  # temporary, gone on exit
            "precompute": False, "gpu": False, "gpu_kernels": False, "device": -1,
            "output_format": "ecsv", "refine_iterations": refine_iterations,
        }
        started = time.monotonic()
        ingested = ingest_results_dir(session, results_dir, run_id, output_format="ecsv", force=force, params=params)
        summary.ingest_seconds = time.monotonic() - started
    summary.n_results = ingested.n_results

    fa_version, numba_version = _versions()
    if force:
        from sqlalchemy import delete

        session.execute(delete(SearchRun).where(SearchRun.run_id == run_id))
    session.add(SearchRun(
        run_id=run_id, seed=seed, shutter_offset_seconds=shutter_offset_seconds,
        threshold=detector.threshold, polarity=detector.polarity, n_exposures=summary.n_exposures,
        n_detections=summary.n_detections, n_results=summary.n_results,
        find_asteroids_version=fa_version, numba_version=numba_version, numpy_version=np.__version__,
        n_cpus=allocated_cpus(), detect_seconds=summary.detect_seconds, search_seconds=summary.search_seconds,
        ingest_seconds=summary.ingest_seconds, created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    session.commit()
    summary.ran = True

    if remove_catalogs_after:
        from deep_asteroids.search.detect import catalog_path

        summary.n_catalogs_removed = remove_catalogs(
            catalog_dir, [catalog_path(catalog_dir, e.deep_db_detector_exposure_id) for e in search_definition.exposures]
        )
    return summary
