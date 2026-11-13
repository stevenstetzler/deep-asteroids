"""Builders for dedup/crossmatch tests: search definitions with exposure
grids, find-asteroids runs holding hand-placed result tracklets, and fitted
known tracklets, all written straight into the database."""
from datetime import datetime

import numpy as np
from find_asteroids.models import Result, Search, Tracklet

from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure
from deep_asteroids.database.tracklets import KnownTracklet
from deep_asteroids.ingest.fit import utc_to_tai

NIGHT = 20190402
T0_UTC = 58576.2  # first exposure midpoint, UTC MJD
CADENCE = 0.0104  # ~15 minutes
N_EXPOSURES = 10
EXPOSURES_UTC = [T0_UTC + i * CADENCE for i in range(N_EXPOSURES)]
EXPOSURES_TAI = [float(utc_to_tai(t)) for t in EXPOSURES_UTC]
T_REF = EXPOSURES_TAI[N_EXPOSURES // 2]  # a tracklet is described by its position at this TAI time
BASE = (215.0, -13.0)
ARCSEC = 1.0 / 3600


def add_definition(session, run_id, field="A0b", detector=1, night=NIGHT, band=None, footprint=None):
    sd = SearchDefinition(
        night=night, field=field, detector_number=detector, detector_name=f"D{detector}", run_id=run_id,
        band=band, n_exposures=N_EXPOSURES, t_start_mjd=EXPOSURES_UTC[0], t_end_mjd=EXPOSURES_UTC[-1],
        created_at=datetime(2026, 1, 1),
    )
    corners = {}
    if footprint is not None:  # (ra, dec, half_size): a square chip, half_size deg on a side from its centre
        ra_c, dec_c, half = footprint
        dra = half / np.cos(np.radians(dec_c))
        corners = {"ra_00": ra_c - dra, "dec_00": dec_c - half, "ra_01": ra_c - dra, "dec_01": dec_c + half,
                   "ra_10": ra_c + dra, "dec_10": dec_c - half, "ra_11": ra_c + dra, "dec_11": dec_c + half}
    for i, t in enumerate(EXPOSURES_UTC):
        sd.exposures.append(SearchDefinitionExposure(
            deep_db_exposure_id=100 + i, deep_db_detector_exposure_id=1000 * detector + i, expnum=5000 + i,
            midpoint_mjd=t, **corners))
    session.add(sd)
    session.flush()
    return sd


def add_run(session, run_id, results):
    """A find-asteroids run holding one result per entry of ``results``: dicts with the tracklet's
    ``ra``/``dec`` (deg, at ``T_REF``), ``vra``/``vdec`` (deg/day), and optionally ``n`` (Hough
    points). Returns the new ``Result`` rows, in order."""
    search = Search(run_id=run_id, velocity_0=0.1, velocity_1=0.5)
    session.add(search)
    session.flush()
    rows = []
    for num, r in enumerate(results):
        result = Result(search_id=search.id, result_num=num, n=r.get("n", 10), n1=0, n2=0, n5=0, n10=0, extra={})
        session.add(result)
        session.flush()
        session.add(Tracklet(
            result_id=result.id, vra=r["vra"], vdec=r["vdec"],
            ra_0=(r["ra"] - r["vra"] * T_REF) % 360.0, dec_0=r["dec"] - r["vdec"] * T_REF, tref=T_REF,
            tmin=EXPOSURES_TAI[0], tmax=EXPOSURES_TAI[-1], extra={}))
        rows.append(result)
    session.flush()
    return rows


def add_gathered(session, result, offsets_arcsec, mags=None, vra=0.2, vdec=0.05, ra=BASE[0], dec=BASE[1]):
    """Gathered points for ``result``: one per exposure, ``offsets_arcsec[i]`` due north of the tracklet
    ``ra``/``dec`` (at ``T_REF``, moving at ``vra``/``vdec``), with magnitude ``mags[i]``."""
    from find_asteroids.models import Gathered

    for i, t in enumerate(EXPOSURES_TAI):
        p_ra, p_dec = ra + vra * (t - T_REF), dec + vdec * (t - T_REF) + offsets_arcsec[i] * ARCSEC
        session.add(Gathered(result_id=result.id, ra=p_ra % 360.0, dec=p_dec, time=t,
                             mag=None if mags is None else mags[i], extra={}))
    session.flush()


def add_known(session, source, ra, dec, vra, vdec, night=NIGHT, flag=None, **identity):
    """A known tracklet whose position is ``ra``/``dec`` at ``T_REF``."""
    fit = flag is None or flag == "large_residual"
    k = KnownTracklet(
        source=source, night=night, epoch=T_REF if fit else None, ra_epoch=ra if fit else None,
        dec_epoch=dec if fit else None, vra=vra if fit else None, vdec=vdec if fit else None,
        t_min=EXPOSURES_TAI[0], t_max=EXPOSURES_TAI[-1], n_points=N_EXPOSURES, flag=flag,
        created_at=datetime(2026, 1, 1), **identity)
    session.add(k)
    session.flush()
    return k


def offset(ra, dec, east_arcsec=0.0, north_arcsec=0.0):
    """A position displaced by the given on-sky amounts."""
    return ra + east_arcsec * ARCSEC / np.cos(np.radians(dec)), dec + north_arcsec * ARCSEC
