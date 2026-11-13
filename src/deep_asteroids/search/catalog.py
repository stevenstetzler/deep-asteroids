"""
Stack a search definition's per-image catalogs into the table
``find_asteroids.search.run_search`` reads.

Every row's ``time`` comes from deep-db's exposure midpoint (UTC, from
``search_definition_exposure``) plus a ``shutter_offset_seconds`` constant, not
from the detection engine's own VisitInfo-derived MJD: one source of truth,
with the engine's value only checked against it. The previous (salad) run's
catalogs were labelled deep-db's midpoint + 0.5 s (a DECam shutter latency its
engine adds), so reproducing it needs ``shutter_offset_seconds=0.5``; the
default is 0, for when deep-db's midpoint already includes the latency.
"""
import logging
from dataclasses import dataclass

import astropy.units as u
import numpy as np
from astropy.table import Table, vstack
from astropy.time import Time

from deep_asteroids.search.detect import catalog_path

log = logging.getLogger(__name__)

# the engine's MJD and our time label should agree to far better than this
MAX_TIME_DISCREPANCY_SECONDS = 0.01


@dataclass
class Catalog:
    table: Table
    n_per_exposure: list  # rows contributed by each exposure, in time order
    max_time_discrepancy_seconds: float  # largest |engine MJD - our label| over the exposures


def build_catalog(search_definition, catalog_dir, shutter_offset_seconds=0.0):
    """The stacked catalog for ``search_definition``, exposures in time order
    (the order find-asteroids sees them in matters for tie-breaking, so it is
    deterministic): ``ra``/``dec`` in degrees, ``time`` a UTC ``Time``,
    ``flux``/``mag`` (dropped if no row has one), and the
    ``deep_db_detector_exposure_id`` it came from. Raises if a catalog is
    missing or no exposure has any detection."""
    exposures = sorted(search_definition.exposures, key=lambda e: e.midpoint_mjd)
    tables, counts, worst = [], [], 0.0
    for e in exposures:
        path = catalog_path(catalog_dir, e.deep_db_detector_exposure_id)
        if not path.exists():
            raise FileNotFoundError(f"{path} not found -- run detection first")
        t = Table.read(path, format="ascii.ecsv")
        label = e.midpoint_mjd + shutter_offset_seconds / 86400.0
        engine_mjd = t.meta.get("MJD")
        if engine_mjd is not None:
            worst = max(worst, abs(engine_mjd - label) * 86400.0)
        counts.append(len(t))
        if len(t) == 0:
            continue  # an empty table can't be stacked reliably, and adds nothing
        out = Table({
            "ra": np.asarray(t["ra"], dtype=float), "dec": np.asarray(t["dec"], dtype=float),
            "time": np.full(len(t), label), "flux": np.asarray(t["flux"], dtype=float),
            "mag": np.asarray(t["mag"], dtype=float),
            "deep_db_detector_exposure_id": np.full(len(t), e.deep_db_detector_exposure_id),
        })
        tables.append(out)
    if not tables:
        raise ValueError(f"{search_definition.run_id}: no detections in any exposure")
    if worst > MAX_TIME_DISCREPANCY_SECONDS:
        log.warning(
            "%s: the detection engine's exposure times differ from deep-db's midpoint + %s s by up to "
            "%.3f s -- check --shutter-offset-seconds", search_definition.run_id, shutter_offset_seconds, worst)

    table = vstack(tables, metadata_conflicts="silent")
    table["ra"] = table["ra"] * u.deg
    table["dec"] = table["dec"] * u.deg
    table["time"] = Time(table["time"], format="mjd", scale="utc")
    for name in ("flux", "mag"):
        if bool(np.all(np.isnan(table[name]))):
            del table[name]  # no value anywhere: leave gathered.flux/mag NULL, not a column of NaN
    return Catalog(table=table, n_per_exposure=counts, max_time_discrepancy_seconds=worst)
