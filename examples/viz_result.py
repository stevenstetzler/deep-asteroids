"""Forced photometry, a light curve, a coadd, a movie and a sky plot for one ingested
find-asteroids result, at its own tracklet's predicted position in every
exposure of its search. The result analog of ``deep-asteroids viz tracklet``,
reusing that module's measuring and rendering.

usage: viz_result.py RUN_ID RESULT_NUM OUT_DIR [--annotate TEMPLATE] [--shutter-offset-seconds S]

Each exposure's tracklet time is deep-db's UTC midpoint converted to TAI, plus
``--shutter-offset-seconds`` (default 0.5): salad's detection engine labels each
exposure 0.5 s after its midpoint (DECam shutter latency), and its results'
times carry that label. Measured against synthetic objects, the results'
times are otherwise correct to within a fraction of a second
(``time_offset_synthetic.py``). Pass 0 for results from a search that does not
add the 0.5 s.

``sky.png`` is salad's sky plot (``salad/viz.py::render_sky_plot``): the result's
gathered (observed) detections colored by time, with its tracklet's predicted
track as an arrow over the searched time span and an x at the predicted
position in every exposure.

Needs ``ingest searches``, ``ingest images`` and ``ingest results`` already
done (``result_ccd41_24.sh`` does all of them). Only the four images are
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
from deep_asteroids.ingest.fit import utc_to_tai
from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.util.cutout import LSSTCutoutPhotometry, PhotometryRequest
from deep_asteroids.viz import tracklet as viz
from find_asteroids.models import Gathered, Result, Search, Tracklet

def predict(t, tt):
    """The tracklet's (ra, dec) at its own time ``tt`` (MJD TAI; ``ra_0`` is wrapped to [0, 360))."""
    return (t.ra_0 + t.vra * tt) % 360, t.dec_0 + t.vdec * tt


def render_sky_plot(t, gathered, exposure_times, out_dir):
    """``sky.png``: gathered detections (colored by time) vs. the tracklet's predicted track."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import FancyArrowPatch

    ra, dec, time = (np.array(c) for c in zip(*gathered))
    t0 = float(time.min())
    fig, ax = plt.subplots(figsize=(14, 6), dpi=110)
    sc = ax.scatter(ra, dec, c=time - t0, cmap="viridis", label="observed (gathered)", zorder=2)
    fig.colorbar(sc, ax=ax).set_label(f"time since {t0:.5f} MJD TAI (day)")

    # the searched span (every exposure), widened to cover the gathered detections: find-asteroids'
    # tmin/tmax come from pre-refinement hits and can undershoot/overshoot it
    tmin, tmax = min(min(exposure_times), time.min()), max(max(exposure_times), time.max())
    (ra0, dec0), (ra1, dec1) = predict(t, tmin), predict(t, tmax)
    ax.add_patch(FancyArrowPatch((ra0, dec0), (ra1, dec1), color="C0", alpha=0.6, linewidth=3,
                                 arrowstyle="-|>", mutation_scale=20, zorder=1))
    pred = np.array([predict(t, tt) for tt in exposure_times])
    ax.plot(pred[:, 0], pred[:, 1], "x", color="C3", markersize=5, label="predicted (tracklet), per exposure", zorder=3)

    # explicit limits: FancyArrowPatch's extent depends on the renderer, so autoscale can clip the arrow
    xs, ys = np.r_[ra, pred[:, 0], ra0, ra1], np.r_[dec, pred[:, 1], dec0, dec1]
    xpad, ypad = (xs.max() - xs.min()) * 0.08 or 1e-4, (ys.max() - ys.min()) * 0.08 or 1e-4
    ax.set_xlim(xs.min() - xpad, xs.max() + xpad)
    ax.set_ylim(ys.min() - ypad, ys.max() + ypad)
    ax.set_xlabel("RA (deg)")
    ax.set_ylabel("Dec (deg)")
    ax.set_title(f"{args.run_id} #{args.result_num}: sky")
    ax.legend(loc="best")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "sky.png"
    fig.savefig(path)
    plt.close(fig)
    return path


parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("run_id")
parser.add_argument("result_num", type=int)
parser.add_argument("out_dir", type=Path)
parser.add_argument("--annotate", default="{night}/{field}/{expnum}")
parser.add_argument("--shutter-offset-seconds", type=float, default=0.5)
args = parser.parse_args()
shutter_days = args.shutter_offset_seconds / 86400


def tracklet_time(midpoint_mjd_utc):
    """The time (MJD TAI) at which a result's tracklet is evaluated for an exposure."""
    return float(utc_to_tai(midpoint_mjd_utc)) + shutter_days

with Session(get_db()) as session:
    t = session.scalars(
        select(Tracklet).join(Result).join(Search)
        .where(Search.run_id == args.run_id, Result.result_num == args.result_num)
    ).one()
    gathered = session.execute(
        select(Gathered.ra, Gathered.dec, Gathered.time).join(Result).join(Search)
        .where(Search.run_id == args.run_id, Result.result_num == args.result_num).order_by(Gathered.time)
    ).all()
    sd = session.scalars(select(SearchDefinition).where(
        SearchDefinition.run_id == args.run_id)).one()
    images = {i.deep_db_detector_exposure_id: i for i in session.scalars(select(Image).where(
        Image.product_type == "differenceExp",
        Image.deep_db_detector_exposure_id.in_([e.deep_db_detector_exposure_id for e in sd.exposures])))}
    exposures = [e for e in sd.exposures if t.tmin - 0.01 <= tracklet_time(e.midpoint_mjd) <= t.tmax + 0.01]

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
        tt = tracklet_time(e.midpoint_mjd)
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
        ("sky", render_sky_plot(t, gathered, [tracklet_time(e.midpoint_mjd) for e in exposures], out)),
    ):
        if path is not None:
            summary.artifacts[kind] = str(path)

print(summary.format())
