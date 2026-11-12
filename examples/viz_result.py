"""Forced photometry, a light curve, a coadd and a movie for one ingested
find-asteroids result, at its own tracklet's predicted position in every
exposure of its search. The result analog of ``deep-asteroids viz tracklet``,
reusing that module's measuring and rendering.

usage: viz_result.py RUN_ID RESULT_NUM OUT_DIR [--annotate TEMPLATE] [--time-offset-seconds S]

``--time-offset-seconds`` (default 32.184) is salad's timestamp offset: its
tracklet times are 32.184 s earlier than UTC midpoints (see the README), so
each exposure's tracklet time is ``utc_midpoint - offset``. Pass 0 for
results that don't come from salad.

Needs ``ingest searches``, ``ingest images`` and ``ingest results`` already
done (``result_ccd41_24.sh`` does all of them). Only the three images are
kept in OUT_DIR; the per-exposure cutouts go to a temporary directory.
"""
import argparse
import collections
import tempfile
from pathlib import Path
from types import SimpleNamespace

from astropy.io import fits
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.database import get_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.util.cutout import LSSTCutoutPhotometry, PhotometryRequest
from deep_asteroids.viz import tracklet as viz
from find_asteroids.models import Result, Search, Tracklet

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("run_id")
parser.add_argument("result_num", type=int)
parser.add_argument("out_dir", type=Path)
parser.add_argument("--annotate", default="{night}/{field}/{expnum}")
parser.add_argument("--time-offset-seconds", type=float, default=32.184)
args = parser.parse_args()
offset_days = args.time_offset_seconds / 86400

with Session(get_db()) as session:
    t = session.scalars(
        select(Tracklet).join(Result).join(Search)
        .where(Search.run_id == args.run_id, Result.result_num == args.result_num)
    ).one()
    sd = session.scalars(select(SearchDefinition).where(
        SearchDefinition.run_id == args.run_id)).one()
    images = {i.deep_db_detector_exposure_id: i for i in session.scalars(select(Image).where(
        Image.product_type == "differenceExp",
        Image.deep_db_detector_exposure_id.in_([e.deep_db_detector_exposure_id for e in sd.exposures])))}
    exposures = [e for e in sd.exposures if t.tmin - 0.01 <= e.midpoint_mjd - offset_days <= t.tmax + 0.01]

summary = viz.VizSummary(
    tracklet=SimpleNamespace(source="find-asteroids", night=sd.night, component=None, orbit_id=None,
                             trkid=None, provid=f"{args.run_id} #{args.result_num}"),
    n_exposures_touched=len(exposures),
    n_images_found=sum(e.deep_db_detector_exposure_id in images for e in exposures),
)
engine = LSSTCutoutPhotometry()
with tempfile.TemporaryDirectory(prefix="viz-result-") as cutout_dir:
    for i, e in enumerate(exposures):
        image = images.get(e.deep_db_detector_exposure_id)
        if image is None:
            continue
        tt = e.midpoint_mjd - offset_days  # the tracklet's own time scale
        ra, dec = (t.ra_0 + t.vra * tt) % 360, t.dec_0 + t.vdec * tt
        (rec,) = engine.measure(
            image.uri, [PhotometryRequest(id=f"{i:04d}_{e.deep_db_detector_exposure_id}", ra=ra, dec=dec)],
            cutout_dir=cutout_dir, cutout_size=viz.DEFAULT_CUTOUT_SIZE,
        )
        summary.measurements.append(viz.TrackletMeasurement(
            deep_db_detector_exposure_id=e.deep_db_detector_exposure_id, mjd_tai=tt, ra=ra, dec=dec,
            in_bounds=rec.in_bounds, night=image.night, field=image.field, expnum=image.expnum,
            mag=rec.mag, mag_err_lo=rec.mag_err_lo, mag_err_hi=rec.mag_err_hi, cutout_path=rec.cutout_path,
        ))

    # cutouts clipped by a chip edge have a different shape and can't be stacked: keep the modal shape
    shapes = [(m, fits.getdata(m.cutout_path).shape) for m in summary.measurements if m.cutout_path]
    modal = collections.Counter(shape for _, shape in shapes).most_common(1)[0][0]
    for m, shape in shapes:
        if shape != modal:
            m.cutout_path = None

    out = args.out_dir
    for kind, path in (
        ("light_curve", viz.render_light_curve_plot(summary, out)),
        ("coadd", viz.render_coadd_plot(summary, out)),
        ("movie", viz.render_movie(summary, out, annotate=args.annotate)),
    ):
        if path is not None:
            summary.artifacts[kind] = str(path)

print(summary.format())
