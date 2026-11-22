"""
Positional uncertainty of results against numbered known objects (#46).

For every ``known_tracklet`` with a ``permid`` (a numbered object -- deep-db's
``mpc_linked``/``mpchecker`` rows carry one when the designation resolves to
a numbered minor planet) that a result matched within ``match_radius_arcsec``
(the stored ``crossmatch`` candidates), compare the known tracklet's
predicted position against each of that result's own ``gathered`` detections
at the detection's own time, keeping only those within
``detection_radius_arcsec``. ``dra_arcsec``/``ddec_arcsec`` are the on-sky
RA/Dec residuals (``dra`` already scaled by cos(dec), so both are directly
comparable and summable in quadrature); ``mag`` is the detection's own
photometry, carried through for the "residual vs. magnitude" plot, where the
object's value is the median over its own kept detections (``results/#46``).
"""
from dataclasses import dataclass

import numpy as np
from find_asteroids.models import Gathered, Result, Search
from sqlalchemy import select

from deep_asteroids.database.crossmatch import CrossmatchCandidate
from deep_asteroids.database.search import SearchDefinition, band_clause
from deep_asteroids.database.tracklets import KnownTracklet
from deep_asteroids.ingest.fit import wrap180

DEFAULT_MATCH_RADIUS_ARCSEC = 2.0  # the submitted/known crossmatch radius (#42)
DEFAULT_DETECTION_RADIUS_ARCSEC = 1.0


@dataclass
class ResidualRow:
    known_tracklet_id: int
    result_id: int
    night: int
    permid: str
    time: float  # MJD TAI
    mag: float  # the detection's own photometry; None if the catalog carried none
    dra_arcsec: float  # (observed - predicted) RA, scaled by cos(dec)
    ddec_arcsec: float  # (observed - predicted) Dec


def _pairs(session, nights, run_ids, match_radius_arcsec, band):
    """``[(result_id, known_tracklet_id, night, permid, epoch, ra_epoch, dec_epoch, vra, vdec)]`` of
    every crossmatch candidate within ``match_radius_arcsec`` whose known tracklet is numbered."""
    q = (
        select(CrossmatchCandidate.result_id, KnownTracklet.id, KnownTracklet.night, KnownTracklet.permid,
               KnownTracklet.epoch, KnownTracklet.ra_epoch, KnownTracklet.dec_epoch, KnownTracklet.vra,
               KnownTracklet.vdec)
        .join(KnownTracklet, KnownTracklet.id == CrossmatchCandidate.known_tracklet_id)
        .where(KnownTracklet.permid.is_not(None), CrossmatchCandidate.mean_sep_arcsec < match_radius_arcsec)
    )
    if nights:
        q = q.where(KnownTracklet.night.in_(list(nights)))
    if run_ids or band is not Ellipsis:
        sub = select(Result.id).join(Search, Search.id == Result.search_id)
        if run_ids:
            sub = sub.where(Search.run_id.in_(list(run_ids)))
        if band is not Ellipsis:
            sub = sub.join(SearchDefinition, SearchDefinition.run_id == Search.run_id).where(band_clause(band))
        q = q.where(CrossmatchCandidate.result_id.in_(sub))
    return session.execute(q.order_by(KnownTracklet.night, CrossmatchCandidate.result_id)).all()


def position_residuals(session, nights=None, run_ids=None, match_radius_arcsec=DEFAULT_MATCH_RADIUS_ARCSEC,
                       detection_radius_arcsec=DEFAULT_DETECTION_RADIUS_ARCSEC, band=Ellipsis):
    """``ResidualRow``s of every detection, of every result matched to a numbered known object,
    within ``detection_radius_arcsec`` of that object's predicted position at the detection's time."""
    pairs = _pairs(session, nights, run_ids, match_radius_arcsec, band)
    if not pairs:
        return []
    out = []
    for chunk in range(0, len(pairs), 500):
        batch = pairs[chunk:chunk + 500]
        result_ids = [p[0] for p in batch]
        gathered = {}
        for row in session.execute(select(Gathered.result_id, Gathered.ra, Gathered.dec, Gathered.time, Gathered.mag)
                                   .where(Gathered.result_id.in_(result_ids)).order_by(Gathered.result_id)):
            gathered.setdefault(row.result_id, []).append((row.ra, row.dec, row.time, row.mag))
        for result_id, kt_id, night, permid, epoch, ra_epoch, dec_epoch, vra, vdec in batch:
            points = gathered.get(result_id)
            if not points:
                continue
            obs_ra, obs_dec, t, mag = (np.array(v, dtype=float) for v in zip(*points))
            pred_ra = (ra_epoch + vra * (t - epoch)) % 360.0
            pred_dec = dec_epoch + vdec * (t - epoch)
            dra = wrap180(obs_ra - pred_ra) * np.cos(np.radians(pred_dec)) * 3600.0
            ddec = (obs_dec - pred_dec) * 3600.0
            sep = np.hypot(dra, ddec)
            keep = sep < detection_radius_arcsec
            for i in np.nonzero(keep)[0]:
                out.append(ResidualRow(
                    known_tracklet_id=kt_id, result_id=result_id, night=night, permid=permid, time=float(t[i]),
                    mag=None if np.isnan(mag[i]) else float(mag[i]), dra_arcsec=float(dra[i]), ddec_arcsec=float(ddec[i])))
    return out


def median_mag_by_object(rows):
    """``{known_tracklet_id: median mag}`` over each object's rows that carry one (``results/#46``'s
    "median magnitude of the gathered detections within 1 arcsec"). An object with none is omitted."""
    by_object = {}
    for r in rows:
        if r.mag is not None:
            by_object.setdefault(r.known_tracklet_id, []).append(r.mag)
    return {kt_id: float(np.median(mags)) for kt_id, mags in by_object.items()}


def write_residuals_csv(rows, out):
    """Writes ``rows`` (``position_residuals``'s own return) to ``out`` as a CSV, one row per
    detection; returns ``out``."""
    import csv

    with open(out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["known_tracklet_id", "result_id", "night", "permid", "time", "mag", "dra_arcsec", "ddec_arcsec"])
        for r in rows:
            writer.writerow([r.known_tracklet_id, r.result_id, r.night, r.permid, r.time, r.mag, r.dra_arcsec, r.ddec_arcsec])
    return out
