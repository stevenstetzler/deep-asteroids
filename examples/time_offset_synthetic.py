"""Measure the time offset between ingested find-asteroids results and deep-db's synthetic
objects, which are exact truth: injected at the ephemeris position at each exposure's midpoint.

usage: time_offset_synthetic.py NIGHT [--max-sep-arcsec 3] [--min-speed 0.1]

Each result whose tracklet matches a synthetic (primary) known_tracklet to within
--max-sep-arcsec (mean over the search's exposures; results evaluated at deep-db's UTC
midpoint converted to TAI, with no offset applied) is projected on the object's direction
of motion: along / speed is the time by which the result's time label is late (+) or
early (-) relative to the truth. Only objects faster than --min-speed deg/day are used,
since a slow object's position says little about time.

Needs ``ingest searches``, ``ingest tracklets synthetic`` and ``ingest results`` for NIGHT.
Prints the median offset, which for salad's results is about -0.2 s: the 0.5 s shutter
label its detection engine adds (see README), and nothing more.
"""
import argparse

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.database import get_db
from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.database.tracklets import KnownTracklet
from deep_asteroids.ingest.fit import utc_to_tai
from find_asteroids.models import Result, Search, Tracklet

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("night", type=int)
parser.add_argument("--max-sep-arcsec", type=float, default=3.0)
parser.add_argument("--min-speed", type=float, default=0.1, help="deg/day")
args = parser.parse_args()

offsets, seps = [], []
with Session(get_db()) as session:
    known = np.array(session.execute(
        select(KnownTracklet.epoch, KnownTracklet.ra_epoch, KnownTracklet.dec_epoch, KnownTracklet.vra,
               KnownTracklet.vdec).where(
            KnownTracklet.source == "synthetic", KnownTracklet.night == args.night,
            KnownTracklet.component == "primary", KnownTracklet.epoch.is_not(None))
    ).all(), float)
    searches = {sd.run_id: sd for sd in session.scalars(
        select(SearchDefinition).where(SearchDefinition.night == args.night, SearchDefinition.band.is_(None)))}
    results = session.execute(
        select(Search.run_id, Tracklet).join(Result, Result.search_id == Search.id)
        .join(Tracklet, Tracklet.result_id == Result.id).where(Search.run_id.in_(list(searches)))).all()

    for run_id, t in results:
        times = utc_to_tai(np.array([e.midpoint_mjd for e in searches[run_id].exposures]))
        in_span = (times >= t.tmin - 0.01) & (times <= t.tmax + 0.01)
        if in_span.sum() < 20:
            continue
        mid = times.mean()
        res_ra = lambda tt: (t.ra_0 + t.vra * tt) % 360
        res_dec = lambda tt: t.dec_0 + t.vdec * tt
        cosd = np.cos(np.radians(res_dec(mid)))
        # coarse spatial prefilter at the middle exposure
        k_ra = (known[:, 1] + known[:, 3] * (mid - known[:, 0])) % 360
        k_dec = known[:, 2] + known[:, 4] * (mid - known[:, 0])
        near = np.where((np.abs((k_ra - res_ra(mid) + 180) % 360 - 180) * cosd < 0.02)
                        & (np.abs(k_dec - res_dec(mid)) < 0.02))[0]
        for i in near:
            epoch, ra0, dec0, vra, vdec = known[i]
            d_ra = ((res_ra(times) - (ra0 + vra * (times - epoch)) + 180) % 360 - 180) * cosd * 3600
            d_dec = (res_dec(times) - (dec0 + vdec * (times - epoch))) * 3600
            sep = np.hypot(d_ra, d_dec)[in_span]
            speed = np.hypot(vra * cosd, vdec)  # deg/day
            if sep.mean() > args.max_sep_arcsec or speed < args.min_speed:
                continue
            u = np.array([vra * cosd, vdec]) / speed
            along = (d_ra[in_span] * u[0] + d_dec[in_span] * u[1]).mean()  # arcsec
            offsets.append(along / (speed * 3600 / 86400))  # arcsec / (arcsec/s) = s
            seps.append(sep.mean())

d = np.array(offsets)
if not len(d):
    raise SystemExit("no matched synthetic objects")
mad = 1.4826 * np.median(abs(d - np.median(d)))
print(f"{len(d)} matched synthetic objects (mean sep < {args.max_sep_arcsec}\", speed > {args.min_speed} deg/day)")
print(f"median mean separation: {np.median(seps):.3f}\"")
print(f"implied time offset (s; result late +, early -): median {np.median(d):.2f}, mean {d.mean():.2f}, "
      f"16-84%: {np.percentile(d, 16):.1f} to {np.percentile(d, 84):.1f}, "
      f"standard error of the median ~{mad / np.sqrt(len(d)):.2f}")
