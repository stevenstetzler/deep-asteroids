"""
How much of what a search could have found did it find (#40's "known objects not found"), as a
function of magnitude where one exists.

For synthetic objects (their positions and magnitudes are exact truth), mpchecker's known
objects, and submitted MPC observations (``mpc_linked``, #28's linked tracklets -- no truth
magnitude, so these are reported as counts/a listing, not a magnitude-binned plot). An object
counts toward the denominator only if the search could have found it:

* its fitted track passes over a **searched chip** at enough of that search's
  exposures: ``min_exposure_fraction`` (default half) of them, tested
  geometrically against each exposure's ``search_definition_exposure`` chip
  footprint -- so objects on chips, fields or nights that were never searched do
  not count as misses; and
* its speed ``hypot(vra, vdec)`` is inside the search's velocity range (a search is
  not expected to find what it was not looking for).

It is **recovered** if a result matched it: a ``crossmatch`` candidate for the
object below the source's radius (2" for synthetic, mpchecker and mpc_linked), from any search.
Its magnitude is ``known_tracklet.mag``, the median truth magnitude of its points that night
(``NaN`` for ``mpc_linked``, which has none). An object on several searched chips is counted
once, for the chip it spends the most exposures on. Binomial uncertainties are Wilson 68%
intervals.
"""
from dataclasses import dataclass

import numpy as np
from find_asteroids.models import Result, Search
from sqlalchemy import select

from deep_asteroids.crossmatch.geometry import exposure_times_tai, positions
from deep_asteroids.database.crossmatch import CrossmatchCandidate
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure, band_clause
from deep_asteroids.database.tracklets import KnownTracklet
from deep_asteroids.ingest.fit import wrap180

DEFAULT_RADII = {"synthetic": 2.0, "mpchecker": 2.0, "mpc_linked": 2.0}
# sources with a truth magnitude -- the others (mpc_linked) have none, so completeness_rows
# does not require one to count an object, and its own rows carry mag=NaN
MAGNITUDE_SOURCES = ("synthetic", "mpchecker")
# a synthetic binary's derived centers are not objects of their own
SYNTHETIC_COMPONENTS = ("primary", "companion")


@dataclass
class CompletenessRow:
    known_tracklet_id: int
    night: int
    mag: float
    speed: float
    run_id: str  # the searched chip it spends the most exposures on
    n_on_chip: int
    n_exposures: int
    recovered: bool


def inside_quadrilaterals(ra, dec, corner_ra, corner_dec):
    """Whether each ``(ra, dec)`` (arrays ``(k, t)``, degrees) lies inside the quadrilateral of the
    ``t``-th exposure's chip: ``corner_ra``/``corner_dec`` are ``(t, 4)`` arrays of its corners in any
    order (they are ordered around the centroid here). Done on the tangent plane of each chip, which is
    exact enough at chip scale (0.15 deg)."""
    rac = np.degrees(np.arctan2(np.mean(np.sin(np.radians(corner_ra)), axis=1),
                                np.mean(np.cos(np.radians(corner_ra)), axis=1)))
    decc = corner_dec.mean(axis=1)
    cos_d = np.cos(np.radians(decc))
    cu = wrap180(corner_ra - rac[:, None]) * cos_d[:, None]
    cv = corner_dec - decc[:, None]
    order = np.argsort(np.arctan2(cv, cu), axis=1)  # counter-clockwise around the centroid
    cu, cv = np.take_along_axis(cu, order, axis=1), np.take_along_axis(cv, order, axis=1)
    pu = wrap180(ra - rac[None, :]) * cos_d[None, :]
    pv = dec - decc[None, :]
    inside = np.ones(ra.shape, dtype=bool)
    for i in range(4):
        j = (i + 1) % 4
        ex, ey = (cu[:, j] - cu[:, i])[None, :], (cv[:, j] - cv[:, i])[None, :]
        inside &= ex * (pv - cv[:, i][None, :]) - ey * (pu - cu[:, i][None, :]) >= 0
    return inside


def completeness_rows(session, source="synthetic", nights=None, run_ids=None, radius=None,
                      min_exposure_fraction=0.5, band=Ellipsis):
    """``CompletenessRow``s for the objects of ``source`` that the selected searches could have found.

    ``band`` restricts to searches of that band (``Ellipsis``: any, ``None``: the undefined-band ones).
    With a band, an object is recovered only by a result of a search of that band; without one, by a
    result of any search."""
    radius = DEFAULT_RADII[source] if radius is None else radius
    q = select(SearchDefinition.id, SearchDefinition.run_id, SearchDefinition.night, Search.velocity_0,
               Search.velocity_1).join(Search, Search.run_id == SearchDefinition.run_id).where(
        Search.id.in_(select(Result.search_id)))
    if nights:
        q = q.where(SearchDefinition.night.in_(list(nights)))
    if run_ids:
        q = q.where(SearchDefinition.run_id.in_(list(run_ids)))
    if (clause := band_clause(band)) is not None:
        q = q.where(clause)
    runs = session.execute(q.order_by(SearchDefinition.run_id)).all()

    known = {}
    for night in sorted({r[2] for r in runs}):
        kq = select(KnownTracklet.id, KnownTracklet.epoch, KnownTracklet.ra_epoch, KnownTracklet.dec_epoch,
                    KnownTracklet.vra, KnownTracklet.vdec, KnownTracklet.mag).where(
            KnownTracklet.source == source, KnownTracklet.night == night, KnownTracklet.epoch.is_not(None))
        if source in MAGNITUDE_SOURCES:
            kq = kq.where(KnownTracklet.mag.is_not(None))
        if source == "synthetic":
            kq = kq.where(KnownTracklet.component.in_(SYNTHETIC_COMPONENTS))
        known[night] = np.array(session.execute(kq.order_by(KnownTracklet.id)).all(), dtype=float).reshape(-1, 7)

    best = {}  # known_tracklet_id -> (n_on_chip, run_id, n_exposures, night, mag, speed)
    for definition_id, run_id, night, v0, v1 in runs:
        k = known[night]
        if not len(k):
            continue
        ex = session.execute(select(SearchDefinitionExposure.midpoint_mjd, SearchDefinitionExposure.ra_00,
                                    SearchDefinitionExposure.dec_00, SearchDefinitionExposure.ra_01,
                                    SearchDefinitionExposure.dec_01, SearchDefinitionExposure.ra_10,
                                    SearchDefinitionExposure.dec_10, SearchDefinitionExposure.ra_11,
                                    SearchDefinitionExposure.dec_11)
                             .where(SearchDefinitionExposure.search_definition_id == definition_id)
                             .order_by(SearchDefinitionExposure.midpoint_mjd)).all()
        ex = np.array([e for e in ex if None not in e], dtype=float).reshape(-1, 9)
        if not len(ex):
            continue
        ex = ex[np.argsort(ex[:, 0])]
        times = exposure_times_tai(ex[:, 0])  # one entry per exposure, in the same order as ``ex``
        if len(times) != len(ex):  # two exposures at one instant cannot be told apart by time
            continue
        speed = np.hypot(k[:, 4], k[:, 5])
        searchable = (speed > v0) & (speed < v1)
        if not searchable.any():
            continue
        k = k[searchable]
        speed = speed[searchable]
        ra, dec = positions(k[:, 2] - k[:, 4] * k[:, 1], k[:, 3] - k[:, 5] * k[:, 1], k[:, 4], k[:, 5], times)
        on = inside_quadrilaterals(ra, dec, ex[:, [1, 3, 5, 7]], ex[:, [2, 4, 6, 8]]).sum(axis=1)
        need = max(1, int(np.ceil(min_exposure_fraction * len(times))))
        for i in np.nonzero(on >= need)[0]:
            kid = int(k[i, 0])
            if kid not in best or on[i] > best[kid][0]:
                best[kid] = (int(on[i]), run_id, len(times), night, float(k[i, 6]), float(speed[i]))

    recovered = set()
    if best:
        ids = list(best)
        in_band = None
        if band is not Ellipsis:  # only results of this band's searches count as a recovery
            in_band = select(Result.id).where(Result.search_id.in_(
                select(Search.id).where(Search.run_id.in_(select(SearchDefinition.run_id).where(band_clause(band))))))
        for chunk in range(0, len(ids), 500):
            q = select(CrossmatchCandidate.known_tracklet_id).where(
                CrossmatchCandidate.known_tracklet_id.in_(ids[chunk:chunk + 500]),
                CrossmatchCandidate.mean_sep_arcsec < radius)
            if in_band is not None:
                q = q.where(CrossmatchCandidate.result_id.in_(in_band))
            recovered.update(session.scalars(q.distinct()).all())
    return [CompletenessRow(known_tracklet_id=kid, night=b[3], mag=b[4], speed=b[5], run_id=b[1], n_on_chip=b[0],
                            n_exposures=b[2], recovered=kid in recovered) for kid, b in sorted(best.items())]


def wilson(k, n, z=1.0):
    """The Wilson score interval (default 68%) for ``k`` successes in ``n`` trials: ``(low, high)``,
    clipped to contain ``k / n`` (the interval is centred off it, so at 0 or ``n`` successes it can
    sit fractionally inside it)."""
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return float(min(centre - half, p)), float(max(centre + half, p))


@dataclass
class MissedObject:
    """One findable-but-unrecovered known object (#40), with enough identity to look it up."""
    known_tracklet_id: int
    source: str
    night: int
    run_id: str  # the searched chip it spends the most exposures on
    mag: float  # NaN for mpc_linked (no truth magnitude)
    speed: float
    n_on_chip: int
    n_exposures: int
    label: str  # the best identity string available for this source (see `_label`)


def _label(kt_id, source, provid, permid, name, designations, orbit_id, component, trkid):
    """The most specific identity string available for a ``known_tracklet`` row. ``synthetic``
    objects are identified by their deep-db orbit (no provid/name of their own); ``mpchecker`` and
    ``mpc_linked`` by whatever designation deep-db attributes to them, falling back to the
    linked tracklet's own ``trkid`` (#28), then the row id, when neither is available."""
    if source == "synthetic":
        return f"orbit {orbit_id} ({component})"
    return permid or name or provid or designations or (f"trkid {trkid}" if trkid else f"known_tracklet {kt_id}")


def missed_known_objects(session, source="synthetic", nights=None, run_ids=None, radius=None,
                         min_exposure_fraction=0.5, band=Ellipsis):
    """The findable objects of ``source`` (``completeness_rows``) that were NOT recovered, each
    with an identity label. The "check for known objects that weren't found" of #40."""
    rows = [r for r in completeness_rows(session, source=source, nights=nights, run_ids=run_ids,
                                         radius=radius, min_exposure_fraction=min_exposure_fraction, band=band)
            if not r.recovered]
    identity = {}
    if rows:
        ids = [r.known_tracklet_id for r in rows]
        for chunk in range(0, len(ids), 500):
            for row in session.execute(
                    select(KnownTracklet.id, KnownTracklet.provid, KnownTracklet.permid, KnownTracklet.name,
                          KnownTracklet.designations, KnownTracklet.orbit_id, KnownTracklet.component,
                          KnownTracklet.trkid)
                    .where(KnownTracklet.id.in_(ids[chunk:chunk + 500]))):
                identity[row.id] = _label(row.id, source, row.provid, row.permid, row.name, row.designations,
                                          row.orbit_id, row.component, row.trkid)
    return [MissedObject(known_tracklet_id=r.known_tracklet_id, source=source, night=r.night, run_id=r.run_id,
                         mag=r.mag, speed=r.speed, n_on_chip=r.n_on_chip, n_exposures=r.n_exposures,
                         label=identity.get(r.known_tracklet_id, f"known_tracklet {r.known_tracklet_id}"))
           for r in sorted(rows, key=lambda r: (r.night, r.run_id, r.known_tracklet_id))]


def write_missed_csv(missed, out):
    """Writes ``missed`` (``missed_known_objects``'s own return) to ``out`` as a CSV, one row per
    object; returns ``out``."""
    import csv

    with open(out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["known_tracklet_id", "source", "night", "run_id", "label", "mag", "speed",
                         "n_on_chip", "n_exposures"])
        for m in missed:
            writer.writerow([m.known_tracklet_id, m.source, m.night, m.run_id, m.label, m.mag, m.speed,
                             m.n_on_chip, m.n_exposures])
    return out


def bin_completeness(rows, bin_width=0.5, mag_min=None, mag_max=None):
    """``[(lo, hi, n, n_recovered, fraction, low, high)]`` per magnitude bin that has objects."""
    if not rows:
        return []
    mags = np.array([r.mag for r in rows])
    rec = np.array([r.recovered for r in rows])
    lo = np.floor((mags.min() if mag_min is None else mag_min) / bin_width) * bin_width
    hi = (mags.max() if mag_max is None else mag_max) + bin_width
    out = []
    for a in np.arange(lo, hi, bin_width):
        sel = (mags >= a) & (mags < a + bin_width)
        n = int(sel.sum())
        if n:
            k = int(rec[sel].sum())
            low, high = wilson(k, n)
            out.append((float(a), float(a + bin_width), n, k, k / n, low, high))
    return out
