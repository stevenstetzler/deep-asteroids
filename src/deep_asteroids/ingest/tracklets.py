"""
Ingest known-object tracklets from deep-db into ``known_tracklet``.

One linear tracklet (``ingest.fit``) per object per night, for three
deep-db sources:

* ``synthetic``: ``synthetic_object_ephemeris``, one tracklet per
  ``synthetic_object`` (``primary``, or a binary's ``companion``) per night.
  Each binary also gets a ``barycenter`` (mean of the two component
  positions) and a ``photocenter`` (flux-weighted by
  ``synthetic_object_binary.delta_h``) tracklet, computed per exposure from
  the paired component positions.
* ``mpc_observation``: ``mpc_observation``, one tracklet per
  ``mpc_tracklet`` (trkid/trksub) per night.
* ``mpchecker``: ``mpchecker_ephemeris``, one tracklet per ``mpc_object``
  per night, fit on every predicted point (on-chip or not).

Times: ephemerides are evaluated at ``exposure.midpoint_mjd``; MPC
observations use their own ``obstime`` (equal to the exposure midpoint to
within ~0.5 s). Both are UTC in deep-db and converted to TAI here. The
night always comes from the point's exposure.

Ingest is idempotent per (source, night): a night that already has rows
for a source is skipped, unless ``force``, which deletes and re-inserts
them.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import bindparam, delete, insert, select, text

from deep_asteroids.database.tracklets import KnownTracklet
from deep_asteroids.ingest.fit import fit_linear_tracklets, tangent_plane_weighted_mean, utc_to_tai

log = logging.getLogger(__name__)

SOURCES = ("synthetic", "mpc_observation", "mpchecker")
NIGHT_BASE = 10**8  # nights are YYYYMMDD: group label = object_id * NIGHT_BASE + night


@dataclass
class SourceSummary:
    source: str
    nights: list = field(default_factory=list)
    skipped_nights: list = field(default_factory=list)
    n_points: int = 0
    n_written: int = 0
    n_flagged: dict = field(default_factory=dict)
    residual_percentiles: dict = field(default_factory=dict)  # by component/kind -> {p50, p90, p99, max}

    def format(self):
        header = (
            f"{self.source}: {len(self.nights)} night(s) ingested, {len(self.skipped_nights)} skipped "
            "(already present; --force to replace)"
        )
        lines = [header]
        if self.nights:
            lines.append(f"  {self.n_points:,} points -> {self.n_written:,} tracklets")
            for flag, n in sorted(self.n_flagged.items()):
                lines.append(f"  flagged {flag}: {n:,}")
            for kind, p in sorted(self.residual_percentiles.items()):
                lines.append(
                    f"  max residual [arcsec] {kind}: p50 {p['p50']:.3g}  p90 {p['p90']:.3g}  "
                    f"p99 {p['p99']:.3g}  max {p['max']:.3g}"
                )
        return "\n".join(lines)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _percentiles(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    p50, p90, p99 = np.percentile(values, [50, 90, 99])
    return {"p50": p50, "p90": p90, "p99": p99, "max": values.max()}


def _fetch_array(deep_engine, query, params, ncols):
    """Run ``query`` and return its (all-numeric) rows as an (n, ncols) float array, streamed."""
    parts = []
    with deep_engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(query, params)
        for part in result.partitions(1_000_000):
            parts.append(np.array(part, dtype=float))
    return np.concatenate(parts) if parts else np.empty((0, ncols))


def _nights_clause(nights):
    return "" if nights is None else "AND n.night IN :nights"


def _bind_nights(query, nights):
    q = text(query)
    return q.bindparams(bindparam("nights", expanding=True)) if nights is not None else q


def _fit_rows(fit, base):
    """KnownTracklet column dicts for each group of a ``fit_linear_tracklets`` result.
    ``base`` is a callable group_label -> dict of identity columns."""
    rows = []
    for i, label in enumerate(fit["group"]):
        fitted = np.isfinite(fit["vra"][i])
        row = {
            "night": int(label % NIGHT_BASE),
            "t_min": float(fit["t_min"][i]),
            "t_max": float(fit["t_max"][i]),
            "n_points": int(fit["n"][i]),
            "epoch": float(fit["epoch"][i]) if fitted else None,
            "ra_epoch": float(fit["ra_epoch"][i]) if fitted else None,
            "dec_epoch": float(fit["dec_epoch"][i]) if fitted else None,
            "vra": float(fit["vra"][i]) if fitted else None,
            "vdec": float(fit["vdec"][i]) if fitted else None,
            "max_residual_arcsec": float(fit["max_residual_arcsec"][i]) if fitted else None,
            "flag": None if fitted else "too_few_points",
        }
        row.update(base(int(label // NIGHT_BASE)))
        rows.append(row)
    return rows


# --- synthetic ---------------------------------------------------------------

SYNTHETIC_POINTS = """
SELECT s.synthetic_object_id, n.night, e.midpoint_mjd, s.ra, s.dec, s.exposure_id
FROM synthetic_object_ephemeris s
JOIN exposure e ON e.id = s.exposure_id
JOIN night n ON n.id = e.night_id
WHERE 1=1 {nights}
"""

SYNTHETIC_OBJECTS = """
SELECT so.id, so.source, so.orbit_id, so.component FROM synthetic_object so
"""

SYNTHETIC_BINARIES = """
SELECT primary_synthetic_object_id, companion_synthetic_object_id, delta_h FROM synthetic_object_binary
"""


def synthetic_rows(deep_engine, nights=None):
    """``known_tracklet`` rows (dicts, without ``source``/provenance) for synthetic objects."""
    points = _fetch_array(
        deep_engine, _bind_nights(SYNTHETIC_POINTS.format(nights=_nights_clause(nights)), nights),
        {"nights": list(nights)} if nights is not None else {}, ncols=6,
    )
    obj, night, mjd_utc, ra, dec, exposure_id = points.T if len(points) else (np.empty(0),) * 6
    obj = obj.astype(np.int64)
    night = night.astype(np.int64)
    exposure_id = exposure_id.astype(np.int64)
    t = utc_to_tai(mjd_utc)

    with deep_engine.connect() as conn:
        objects = {r[0]: r[1:] for r in conn.execute(text(SYNTHETIC_OBJECTS))}
        binaries = conn.execute(text(SYNTHETIC_BINARIES)).fetchall()

    def identity(object_id):
        source, orbit_id, component = objects[object_id]
        return {"deep_db_synthetic_object_id": object_id, "synthetic_source": source,
                "orbit_id": orbit_id, "component": component}

    fit = fit_linear_tracklets(obj * NIGHT_BASE + night, t, ra, dec)
    rows = _fit_rows(fit, identity)
    rows.extend(_binary_center_rows(obj, night, t, ra, dec, exposure_id, objects, binaries))
    return rows, len(points)


def _binary_center_rows(obj, night, t, ra, dec, exposure_id, objects, binaries):
    """Barycenter and photocenter tracklets for each binary, from the
    component positions paired by exposure."""
    if not binaries or len(obj) == 0:
        return []
    primary_ids = np.array([b[0] for b in binaries], dtype=np.int64)
    companion_ids = np.array([b[1] for b in binaries], dtype=np.int64)
    delta_h = {int(b[0]): float(b[2]) for b in binaries}
    companion_to_primary = dict(zip(companion_ids.tolist(), primary_ids.tolist(), strict=True))

    is_primary = np.isin(obj, primary_ids)
    is_companion = np.isin(obj, companion_ids)
    # pair each companion point with its primary's point on the same exposure
    key_p = obj[is_primary] * (1 << 32) + exposure_id[is_primary]
    companion_primary = np.array([companion_to_primary[o] for o in obj[is_companion].tolist()], dtype=np.int64)
    key_c = companion_primary * (1 << 32) + exposure_id[is_companion]
    _, ip, ic = np.intersect1d(key_p, key_c, assume_unique=False, return_indices=True)
    n_unpaired = int(is_primary.sum() + is_companion.sum() - 2 * len(ip))
    if n_unpaired:
        log.warning("synthetic: %d binary component point(s) have no partner on the same exposure", n_unpaired)
    if len(ip) == 0:
        return []

    p = np.flatnonzero(is_primary)[ip]
    c = np.flatnonzero(is_companion)[ic]
    primary = obj[p]
    group = primary * NIGHT_BASE + night[p]
    weight = 10 ** (-0.4 * np.array([delta_h[o] for o in primary.tolist()]))

    rows = []
    for component, w in (("barycenter", np.ones_like(weight)), ("photocenter", weight)):
        cra, cdec = tangent_plane_weighted_mean(ra[p], dec[p], ra[c], dec[c], w)
        fit = fit_linear_tracklets(group, t[p], cra, cdec)

        def identity(primary_id, component=component):
            source, orbit_id, _ = objects[primary_id]
            return {"deep_db_synthetic_object_id": None, "synthetic_source": source,
                    "orbit_id": orbit_id, "component": component}

        rows.extend(_fit_rows(fit, identity))
    return rows


# --- shared ------------------------------------------------------------------

ROW_BUILDERS = {
    "synthetic": synthetic_rows,
}


def deep_db_nights(deep_engine):
    with deep_engine.connect() as conn:
        return sorted(r[0] for r in conn.execute(text("SELECT night FROM night WHERE night IS NOT NULL")))


def ingest_source(session, deep_engine, source, nights=None, force=False, deep_db_sha=None):
    """Ingest one source for ``nights`` (default: every deep-db night).
    Returns a ``SourceSummary``. Commits."""
    if source not in ROW_BUILDERS:
        raise ValueError(f"unknown source {source!r}; expected one of {sorted(ROW_BUILDERS)}")
    summary = SourceSummary(source)
    requested = sorted(set(nights)) if nights else deep_db_nights(deep_engine)
    existing = set(session.scalars(
        select(KnownTracklet.night).where(KnownTracklet.source == source).distinct()
    ))
    if force:
        todo = requested
        if todo:
            session.execute(delete(KnownTracklet).where(
                KnownTracklet.source == source, KnownTracklet.night.in_(todo)
            ))
    else:
        todo = [n for n in requested if n not in existing]
        summary.skipped_nights = [n for n in requested if n in existing]
    summary.nights = todo
    if not todo:
        session.commit()
        return summary

    rows, summary.n_points = ROW_BUILDERS[source](deep_engine, nights=todo)
    created_at = _now()
    for row in rows:
        row["source"] = source
        row["deep_db_sha"] = deep_db_sha
        row["created_at"] = created_at
    if rows:
        session.execute(insert(KnownTracklet), rows)
    session.commit()

    summary.n_written = len(rows)
    for row in rows:
        if row["flag"]:
            summary.n_flagged[row["flag"]] = summary.n_flagged.get(row["flag"], 0) + 1
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row.get("component") or source, []).append(row["max_residual_arcsec"])
    for kind, values in by_kind.items():
        p = _percentiles([np.nan if v is None else v for v in values])
        if p:
            summary.residual_percentiles[kind] = p
    log.info("%s", summary.format())
    return summary
