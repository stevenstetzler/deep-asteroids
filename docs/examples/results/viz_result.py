"""Visualize one ingested find-asteroids result (largest by points) by
reusing deep_asteroids.viz.tracklet's measure/render functions.

Predicted positions come from the result's tracklet (ra_0 + vra*t). Salad's
timestamps are 32.184 s earlier than UTC midpoints, so each exposure's
tracklet time is utc_mid - 32.184 s (see README), not utc_to_tai()."""
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from deep_asteroids.database import get_db, get_deep_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.util.cutout import LSSTCutoutPhotometry, PhotometryRequest
from deep_asteroids.viz import tracklet as viz
from find_asteroids.models import Points, Result, Search, Tracklet

out = Path(sys.argv[1])
SALAD_OFFSET_DAYS = 32.184 / 86400

with Session(get_db()) as s:
    result_id, run_id, result_num, npts = s.execute(
        select(Result.id, Search.run_id, Result.result_num, func.count(Points.id))
        .join(Search, Search.id == Result.search_id).join(Points, Points.result_id == Result.id)
        .group_by(Result.id, Search.run_id, Result.result_num)
        .order_by(func.count(Points.id).desc(), Result.id).limit(1)
    ).one()
    t = s.scalars(select(Tracklet).where(Tracklet.result_id == result_id)).one()
    sd = s.scalars(select(SearchDefinition).where(SearchDefinition.run_id == run_id)).one()
    print(f"result {result_id} ({run_id} #{result_num}): {npts} points; vra={t.vra} vdec={t.vdec}", file=sys.stderr)

    fake = SimpleNamespace(source="find-asteroids", night=sd.night, component=None, orbit_id=None, trkid=None,
                           provid=f"{run_id} #{result_num}")
    summary = viz.VizSummary(tracklet=fake)
    images = {i.deep_db_detector_exposure_id: i for i in s.scalars(
        select(Image).where(Image.product_type == "differenceExp",
                            Image.deep_db_detector_exposure_id.in_([e.deep_db_detector_exposure_id for e in sd.exposures])))}
    engine = LSSTCutoutPhotometry()
    exposures = [e for e in sd.exposures if t.tmin - 0.01 <= e.midpoint_mjd - SALAD_OFFSET_DAYS <= t.tmax + 0.01]
    summary.n_exposures_touched = len(exposures)
    summary.n_images_found = sum(e.deep_db_detector_exposure_id in images for e in exposures)
    for i, e in enumerate(exposures):
        img = images.get(e.deep_db_detector_exposure_id)
        if img is None:
            continue
        tt = e.midpoint_mjd - SALAD_OFFSET_DAYS  # tracklet (salad-TAI) time
        ra, dec = t.ra_0 + t.vra * tt, t.dec_0 + t.vdec * tt
        (rec,) = engine.measure(img.uri, [PhotometryRequest(id=f"{i:04d}_{e.deep_db_detector_exposure_id}", ra=ra % 360, dec=dec)],
                                cutout_dir=str(out), cutout_size=viz.DEFAULT_CUTOUT_SIZE)
        summary.measurements.append(viz.TrackletMeasurement(
            deep_db_detector_exposure_id=e.deep_db_detector_exposure_id, mjd_tai=tt, ra=ra, dec=dec,
            in_bounds=rec.in_bounds, night=img.night, field=img.field, expnum=img.expnum,
            mag=rec.mag, mag_err_lo=rec.mag_err_lo, mag_err_hi=rec.mag_err_hi, cutout_path=rec.cutout_path))
for kind, p in (("light_curve", viz.render_light_curve_plot(summary, out)), ("coadd", viz.render_coadd_plot(summary, out)),
                ("movie", viz.render_movie(summary, out, annotate="{night}/{field}/{expnum}"))):
    print(kind, p)
print(f"{summary.n_exposures_touched} exposures, {summary.n_images_found} images, "
      f"{sum(m.in_bounds for m in summary.measurements)} in bounds, "
      f"{sum(m.mag is not None and m.mag == m.mag for m in summary.measurements)} with mag")
