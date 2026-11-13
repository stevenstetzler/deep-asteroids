"""
Stack a search definition's per-image catalogs into the table
``find_asteroids.search.run_search`` reads.

Every row's ``time`` is deep-db's exposure midpoint (UTC, from
``search_definition_exposure``), the same time the known-object and synthetic
ephemerides were evaluated at, so a catalog's timing matches theirs by
construction. The detection engine's own exposure time (in each catalog
file's header) is not used.
"""
import logging
from dataclasses import dataclass

import astropy.units as u
import numpy as np
from astropy.table import Table, vstack
from astropy.time import Time

from deep_asteroids.search.detect import catalog_path

log = logging.getLogger(__name__)


@dataclass
class Catalog:
    table: Table
    n_per_exposure: list  # rows contributed by each exposure, in time order


def build_catalog(search_definition, catalog_dir):
    """The stacked catalog for ``search_definition``, exposures in time order
    (the order find-asteroids sees them in matters for tie-breaking, so it is
    deterministic): ``ra``/``dec`` in degrees, ``time`` a UTC ``Time``,
    ``flux``/``mag`` (dropped if no row has one), and the
    ``deep_db_detector_exposure_id`` it came from. Raises if a catalog is
    missing or no exposure has any detection."""
    exposures = sorted(search_definition.exposures, key=lambda e: e.midpoint_mjd)
    tables, counts = [], []
    for e in exposures:
        path = catalog_path(catalog_dir, e.deep_db_detector_exposure_id)
        if not path.exists():
            raise FileNotFoundError(f"{path} not found -- run detection first")
        t = Table.read(path, format="ascii.ecsv")
        counts.append(len(t))
        if len(t) == 0:
            continue  # an empty table can't be stacked reliably, and adds nothing
        out = Table({
            "ra": np.asarray(t["ra"], dtype=float), "dec": np.asarray(t["dec"], dtype=float),
            "time": np.full(len(t), e.midpoint_mjd), "flux": np.asarray(t["flux"], dtype=float),
            "mag": np.asarray(t["mag"], dtype=float),
            "deep_db_detector_exposure_id": np.full(len(t), e.deep_db_detector_exposure_id),
        })
        tables.append(out)
    if not tables:
        raise ValueError(f"{search_definition.run_id}: no detections in any exposure")
    table = vstack(tables, metadata_conflicts="silent")
    table["ra"] = table["ra"] * u.deg
    table["dec"] = table["dec"] * u.deg
    table["time"] = Time(table["time"], format="mjd", scale="utc")
    for name in ("flux", "mag"):
        if bool(np.all(np.isnan(table[name]))):
            del table[name]  # no value anywhere: leave gathered.flux/mag NULL, not a column of NaN
    return Catalog(table=table, n_per_exposure=counts)
