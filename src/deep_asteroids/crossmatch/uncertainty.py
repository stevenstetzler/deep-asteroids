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


def _gathered_by_result(session, result_ids):
    """``{result_id: [(ra, dec, time, mag)]}`` of ``result_ids``' gathered detections."""
    gathered = {}
    for chunk in range(0, len(result_ids), 500):
        for row in session.execute(
                select(Gathered.result_id, Gathered.ra, Gathered.dec, Gathered.time, Gathered.mag)
                .where(Gathered.result_id.in_(result_ids[chunk:chunk + 500])).order_by(Gathered.result_id)):
            gathered.setdefault(row.result_id, []).append((row.ra, row.dec, row.time, row.mag))
    return gathered


def _residuals(pairs, gathered, detection_radius_arcsec, time_offset_days=0.0):
    """``[(result_id, kt_id, night, permid, dra_arcsec[kept], ddec_arcsec[kept], t[kept], mag[kept])]``,
    one entry per pair with at least one kept detection -- the shared inner loop of
    ``position_residuals`` and ``time_offset_scan``. ``time_offset_days`` shifts the time the known
    tracklet's fit is *evaluated* at (added to each detection's own time), simulating a timing error
    in the known object's ephemeris (#46's timing half)."""
    out = []
    for result_id, kt_id, night, permid, epoch, ra_epoch, dec_epoch, vra, vdec in pairs:
        points = gathered.get(result_id)
        if not points:
            continue
        obs_ra, obs_dec, t, mag = (np.array(v, dtype=float) for v in zip(*points))
        t_eval = t + time_offset_days
        pred_ra = (ra_epoch + vra * (t_eval - epoch)) % 360.0
        pred_dec = dec_epoch + vdec * (t_eval - epoch)
        dra = wrap180(obs_ra - pred_ra) * np.cos(np.radians(pred_dec)) * 3600.0
        ddec = (obs_dec - pred_dec) * 3600.0
        keep = np.hypot(dra, ddec) < detection_radius_arcsec
        if keep.any():
            out.append((result_id, kt_id, night, permid, dra[keep], ddec[keep], t[keep], mag[keep]))
    return out


def position_residuals(session, nights=None, run_ids=None, match_radius_arcsec=DEFAULT_MATCH_RADIUS_ARCSEC,
                       detection_radius_arcsec=DEFAULT_DETECTION_RADIUS_ARCSEC, band=Ellipsis,
                       time_offset_seconds=0.0):
    """``ResidualRow``s of every detection, of every result matched to a numbered known object,
    within ``detection_radius_arcsec`` of that object's predicted position at the detection's time.
    ``time_offset_seconds`` shifts the known tracklet's evaluation time (see ``_residuals``)."""
    pairs = _pairs(session, nights, run_ids, match_radius_arcsec, band)
    if not pairs:
        return []
    out = []
    for chunk in range(0, len(pairs), 500):
        batch = pairs[chunk:chunk + 500]
        gathered = _gathered_by_result(session, [p[0] for p in batch])
        for result_id, kt_id, night, permid, dra, ddec, t, mag in _residuals(
                batch, gathered, detection_radius_arcsec, time_offset_seconds / 86400.0):
            for i in range(len(dra)):
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


MAD_TO_SIGMA = 1.4826  # scales a Gaussian's MAD to its sigma


def bin_residuals_by_magnitude(rows, median_mag_by_object, bin_width=0.5, min_n=20):
    """Per-magnitude-bin robust stats of ``dra_arcsec``/``ddec_arcsec``, for ``plot_position_residuals_vs_mag``.

    Fixed-width bins (``completeness.bin_completeness``'s own scheme) over each *object's* median
    magnitude (``median_mag_by_object``; a detection's x-value is its object's, repeated) -- not the
    per-detection one, since that is not what the plot's x-axis is. Within a bin, the median and a
    robust sigma (MAD * 1.4826, resistant to the long tail the 1" detection radius still lets through)
    of its detections' dRA/dDec; a bin with fewer than ``min_n`` detections is dropped rather than
    plotted with a noisy estimate. Returns ``[(lo, hi, n, median_dra, sigma_dra, median_ddec, sigma_ddec)]``."""
    pts = [(median_mag_by_object[r.known_tracklet_id], r.dra_arcsec, r.ddec_arcsec)
           for r in rows if r.known_tracklet_id in median_mag_by_object]
    if not pts:
        return []
    mag, dra, ddec = (np.array(v) for v in zip(*pts))
    lo = np.floor(mag.min() / bin_width) * bin_width
    hi = mag.max() + bin_width
    out = []
    for a in np.arange(lo, hi, bin_width):
        sel = (mag >= a) & (mag < a + bin_width)
        n = int(sel.sum())
        if n >= min_n:
            d, dd = dra[sel], ddec[sel]
            med_d, med_dd = float(np.median(d)), float(np.median(dd))
            sigma_d = float(np.median(np.abs(d - med_d))) * MAD_TO_SIGMA
            sigma_dd = float(np.median(np.abs(dd - med_dd))) * MAD_TO_SIGMA
            out.append((float(a), float(a + bin_width), n, med_d, sigma_d, med_dd, sigma_dd))
    return out


@dataclass
class TimeOffsetRow:
    """Summary of the dRA/dDec distributions at one timing offset (``results/timing_uncertainty.py``, #46)."""
    offset_seconds: float
    n: int
    median_dra_arcsec: float
    std_dra_arcsec: float
    median_ddec_arcsec: float
    std_ddec_arcsec: float


def time_offset_scan(session, offsets_seconds, nights=None, run_ids=None,
                     match_radius_arcsec=DEFAULT_MATCH_RADIUS_ARCSEC,
                     detection_radius_arcsec=DEFAULT_DETECTION_RADIUS_ARCSEC, band=Ellipsis):
    """For each of ``offsets_seconds``, redo the detection-level positional comparison with the known
    tracklet's evaluation time shifted by it, and summarize the kept dRA/dDec as a ``TimeOffsetRow``
    (median and standard deviation, pooling every kept detection at that offset) -- how sensitive the
    astrometric agreement is to a timing error in the known objects' ephemerides (#46).

    The crossmatch pairs and their gathered detections are fetched once and reused across every
    offset (only the comparison itself, ``_residuals``, is redone per offset)."""
    pairs = _pairs(session, nights, run_ids, match_radius_arcsec, band)
    if not pairs:
        return []
    gathered = {}
    for chunk in range(0, len(pairs), 500):
        gathered.update(_gathered_by_result(session, [p[0] for p in pairs[chunk:chunk + 500]]))
    out = []
    for offset_s in offsets_seconds:
        dra_all, ddec_all = [], []
        for _result_id, _kt_id, _night, _permid, dra, ddec, _t, _mag in _residuals(
                pairs, gathered, detection_radius_arcsec, float(offset_s) / 86400.0):
            dra_all.append(dra)
            ddec_all.append(ddec)
        dra_all = np.concatenate(dra_all) if dra_all else np.empty(0)
        ddec_all = np.concatenate(ddec_all) if ddec_all else np.empty(0)
        out.append(TimeOffsetRow(
            offset_seconds=float(offset_s), n=len(dra_all),
            median_dra_arcsec=float(np.median(dra_all)) if len(dra_all) else float("nan"),
            std_dra_arcsec=float(dra_all.std()) if len(dra_all) else float("nan"),
            median_ddec_arcsec=float(np.median(ddec_all)) if len(ddec_all) else float("nan"),
            std_ddec_arcsec=float(ddec_all.std()) if len(ddec_all) else float("nan")))
    return out


def write_time_offset_csv(rows, out):
    """Writes ``rows`` (``time_offset_scan``'s own return) to ``out`` as a CSV, one row per offset;
    returns ``out``."""
    import csv

    with open(out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["offset_seconds", "n", "median_dra_arcsec", "std_dra_arcsec",
                         "median_ddec_arcsec", "std_ddec_arcsec"])
        for r in rows:
            writer.writerow([r.offset_seconds, r.n, r.median_dra_arcsec, r.std_dra_arcsec,
                             r.median_ddec_arcsec, r.std_ddec_arcsec])
    return out


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
