"""
How much of what a search could have found did it find, as a function of magnitude.

For synthetic objects (their positions and magnitudes are exact truth) and, with
less certainty, mpchecker's known objects. An object counts toward the
denominator only if the search could have found it:

* its fitted track passes over a **searched chip** at enough of that search's
  exposures: ``min_exposure_fraction`` (default half) of them, tested
  geometrically against each exposure's ``search_definition_exposure`` chip
  footprint -- so objects on chips, fields or nights that were never searched do
  not count as misses; and
* its speed ``hypot(vra, vdec)`` is inside the search's velocity range (a search is
  not expected to find what it was not looking for).

It is **recovered** if a result matched it: a ``crossmatch`` candidate for the
object below the source's radius (2" synthetic, 10" mpchecker), from any search.
Its magnitude is ``known_tracklet.mag``, the median truth magnitude of its points
that night. An object on several searched chips is counted once, for the chip it
spends the most exposures on. Binomial uncertainties are Wilson 68% intervals.
"""
from dataclasses import dataclass

import numpy as np
from find_asteroids.models import Result, Search
from sqlalchemy import select

from deep_asteroids.crossmatch.geometry import exposure_times_tai, positions
from deep_asteroids.database.crossmatch import CrossmatchCandidate
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure
from deep_asteroids.database.tracklets import KnownTracklet
from deep_asteroids.ingest.fit import wrap180

DEFAULT_RADII = {"synthetic": 2.0, "mpchecker": 10.0}
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
                      min_exposure_fraction=0.5):
    """``CompletenessRow``s for the objects of ``source`` that the selected searches could have found."""
    radius = DEFAULT_RADII[source] if radius is None else radius
    q = select(SearchDefinition.id, SearchDefinition.run_id, SearchDefinition.night, Search.velocity_0,
               Search.velocity_1).join(Search, Search.run_id == SearchDefinition.run_id).where(
        Search.id.in_(select(Result.search_id)))
    if nights:
        q = q.where(SearchDefinition.night.in_(list(nights)))
    if run_ids:
        q = q.where(SearchDefinition.run_id.in_(list(run_ids)))
    runs = session.execute(q.order_by(SearchDefinition.run_id)).all()

    known = {}
    for night in sorted({r[2] for r in runs}):
        kq = select(KnownTracklet.id, KnownTracklet.epoch, KnownTracklet.ra_epoch, KnownTracklet.dec_epoch,
                    KnownTracklet.vra, KnownTracklet.vdec, KnownTracklet.mag).where(
            KnownTracklet.source == source, KnownTracklet.night == night, KnownTracklet.epoch.is_not(None),
            KnownTracklet.mag.is_not(None))
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
        for chunk in range(0, len(ids), 500):
            recovered.update(session.scalars(
                select(CrossmatchCandidate.known_tracklet_id).where(
                    CrossmatchCandidate.known_tracklet_id.in_(ids[chunk:chunk + 500]),
                    CrossmatchCandidate.mean_sep_arcsec < radius).distinct()).all())
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
