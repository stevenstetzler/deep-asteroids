"""
Forced photometry, a light curve, a coadd, a movie and a sky plot for one
ingested find-asteroids result -- the result analog of ``viz/tracklet.py``,
reusing its measuring and rendering (``render_light_curve_plot()``/
``render_coadd_plot()``/``render_movie()``). Promoted from the one-off
``examples/viz_result.py`` script (PRs #14/#20) into a CLI primitive
(``deep-asteroids viz result``) for #27.

Each exposure's time is the result's own search definition exposure, at
deep-db's UTC midpoint converted to TAI -- the time the search labelled it
with, and the time the tracklet's linear fit is evaluated at. Only the
touched exposures within the tracklet's own fitted span (``tmin``/``tmax``,
widened slightly) are measured, not the whole search.

Unlike ``visualize_tracklet()`` (which keeps its cutouts under ``out_dir``),
cutouts here are written to a temporary directory and discarded once the
light curve/coadd/movie/sky plots are rendered: only those four images are
kept.
"""
import collections
import logging
import tempfile
from dataclasses import dataclass

from astropy.io import fits
from find_asteroids.models import Gathered, Result, Search, Tracklet
from sqlalchemy import select

from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.ingest.fit import utc_to_tai
from deep_asteroids.util.cutout import PhotometryRequest
from deep_asteroids.viz.tracklet import (
    DEFAULT_CUTOUT_SIZE,
    DEFAULT_PRODUCT_TYPE,
    TrackletMeasurement,
    VizSummary,
    render_coadd_plot,
    render_light_curve_plot,
    render_movie,
)

log = logging.getLogger(__name__)


@dataclass
class ResultIdentity:
    """Stands in for a ``known_tracklet`` row so ``VizSummary.label()``
    works unchanged for a find-asteroids result."""
    source: str
    night: int
    component: str | None = None
    orbit_id: int | None = None
    trkid: str | None = None
    provid: str | None = None


def resolve_result(session, run_id, result_num):
    """The ``(SearchDefinition, Tracklet, [(ra, dec, time), ...])`` for one
    ``run_id``/``result_num``, or ``None`` if no such result exists."""
    t = session.scalars(
        select(Tracklet).join(Result).join(Search)
        .where(Search.run_id == run_id, Result.result_num == result_num)
    ).one_or_none()
    if t is None:
        return None
    sd = session.scalars(select(SearchDefinition).where(SearchDefinition.run_id == run_id)).one()
    gathered = session.execute(
        select(Gathered.ra, Gathered.dec, Gathered.time).join(Result).join(Search)
        .where(Search.run_id == run_id, Result.result_num == result_num).order_by(Gathered.time)
    ).all()
    return sd, t, gathered


def predict(tracklet, mjd_tai):
    """``tracklet``'s (ra, dec) at ``mjd_tai`` (``ra_0``/``vra`` are its
    find-asteroids MJD-0 fit; ``ra`` is wrapped to [0, 360))."""
    return (tracklet.ra_0 + tracklet.vra * mjd_tai) % 360, tracklet.dec_0 + tracklet.vdec * mjd_tai


def touched_exposures(sd, tracklet):
    """``sd``'s own exposures within the tracklet's fitted time span
    (``tmin``/``tmax``, TAI; widened slightly for pre-refinement rounding),
    chronological."""
    return [e for e in sd.exposures
            if tracklet.tmin - 0.01 <= float(utc_to_tai(e.midpoint_mjd)) <= tracklet.tmax + 0.01]


def measure_result(session, engine, sd, tracklet, exposures, product_type=DEFAULT_PRODUCT_TYPE,
                   cutout_dir=None, cutout_size=DEFAULT_CUTOUT_SIZE):
    """Forced photometry (+ a cutout, if ``cutout_dir`` is given) at
    ``tracklet``'s own predicted position on every one of ``exposures``.
    Returns a ``VizSummary`` with ``measurements`` filled in (no plots
    yet)."""
    identity = ResultIdentity(source="find-asteroids", night=sd.night, provid=f"{sd.run_id} result")
    summary = VizSummary(tracklet=identity, n_exposures_touched=len(exposures))
    if not exposures:
        return summary

    detector_exposure_ids = [e.deep_db_detector_exposure_id for e in exposures]
    images_by_detector_exposure = {
        img.deep_db_detector_exposure_id: img
        for img in session.scalars(
            select(Image).where(
                Image.deep_db_detector_exposure_id.in_(detector_exposure_ids), Image.product_type == product_type,
            )
        )
    }
    summary.n_images_found = len(images_by_detector_exposure)

    for i, e in enumerate(exposures):
        image = images_by_detector_exposure.get(e.deep_db_detector_exposure_id)
        if image is None:
            continue
        mjd_tai = float(utc_to_tai(e.midpoint_mjd))
        ra, dec = predict(tracklet, mjd_tai)
        request_id = f"{i:04d}_{e.deep_db_detector_exposure_id}"
        (record,) = engine.measure(
            image.uri, [PhotometryRequest(id=request_id, ra=float(ra), dec=float(dec))],
            cutout_dir=cutout_dir, cutout_size=cutout_size,
        )
        summary.measurements.append(TrackletMeasurement(
            deep_db_detector_exposure_id=e.deep_db_detector_exposure_id, mjd_tai=mjd_tai, ra=float(ra),
            dec=float(dec), in_bounds=record.in_bounds, night=image.night, field=image.field, expnum=image.expnum,
            mag=record.mag, mag_err_lo=record.mag_err_lo, mag_err_hi=record.mag_err_hi,
            cutout_path=record.cutout_path,
        ))
    summary.measurements.sort(key=lambda m: m.mjd_tai)

    # a cutout clipped by a chip edge has a different shape and can't be stacked for the coadd/movie --
    # keep only the modal shape (matches examples/viz_result.py's original behavior)
    shapes = [(m, fits.getdata(m.cutout_path).shape) for m in summary.measurements if m.cutout_path]
    if shapes:
        modal = collections.Counter(shape for _, shape in shapes).most_common(1)[0][0]
        for m, shape in shapes:
            if shape != modal:
                m.cutout_path = None
    return summary


def render_sky_plot(run_id, result_num, tracklet, gathered, exposure_times, out_dir, ax=None, fmt="png",
                    title=True):
    """The result's gathered (observed) detections colored by time, with
    its tracklet's predicted track as an arrow over the searched time
    span and an x at the predicted position in every exposure (salad's
    sky plot, ``salad/viz.py::render_sky_plot``). By default creates and
    saves its own figure (``sky.<fmt>``), returning its path -- or
    ``None`` if there's nothing gathered to plot, or (with ``ax`` given)
    nothing was drawn. With ``ax`` given, draws directly onto it instead
    (the caller owns saving) and always returns ``None`` -- see
    ``viz/tracklet.py``'s ``render_light_curve_plot()``. ``title=False``
    skips the axes title."""
    if not gathered:
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import FancyArrowPatch

    fig = None
    if ax is None:
        fig, ax = plt.subplots(figsize=(14, 6), dpi=110)
    ra, dec, time = (np.array(c) for c in zip(*gathered))
    t0 = float(time.min())
    sc = ax.scatter(ra, dec, c=time - t0, cmap="viridis", label="observed (gathered)", zorder=2)
    ax.figure.colorbar(sc, ax=ax).set_label(f"time since {t0:.5f} MJD TAI (day)")

    tmin, tmax = min([*exposure_times, time.min()]), max([*exposure_times, time.max()])
    (ra0, dec0), (ra1, dec1) = predict(tracklet, tmin), predict(tracklet, tmax)
    ax.add_patch(FancyArrowPatch((ra0, dec0), (ra1, dec1), color="C0", alpha=0.6, linewidth=3,
                                 arrowstyle="-|>", mutation_scale=20, zorder=1))
    pred = np.array([predict(tracklet, tt) for tt in exposure_times]) if exposure_times else np.empty((0, 2))
    if len(pred):
        ax.plot(pred[:, 0], pred[:, 1], "x", color="C3", markersize=5, label="predicted (tracklet), per exposure",
               zorder=3)

    xs, ys = np.r_[ra, pred[:, 0], ra0, ra1], np.r_[dec, pred[:, 1], dec0, dec1]
    xpad, ypad = (xs.max() - xs.min()) * 0.08 or 1e-4, (ys.max() - ys.min()) * 0.08 or 1e-4
    ax.set_xlim(xs.min() - xpad, xs.max() + xpad)
    ax.set_ylim(ys.min() - ypad, ys.max() + ypad)
    ax.set_xlabel("RA (deg)")
    ax.set_ylabel("Dec (deg)")
    if title:
        ax.set_title(f"{run_id} #{result_num}: sky")
    ax.legend(loc="best")

    if fig is None:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"sky.{fmt}"
    fig.savefig(path)
    plt.close(fig)
    return path


def visualize_result(session, engine, run_id, result_num, out_dir, product_type=DEFAULT_PRODUCT_TYPE,
                     cutout_size=DEFAULT_CUTOUT_SIZE, annotate=None, plot_format="png", movie=True):
    """Measure + render everything for one find-asteroids result:
    ``light_curve.<plot_format>``/``coadd.<plot_format>``/``movie.gif``/
    ``sky.<plot_format>`` under ``out_dir`` (``plot_format`` e.g.
    ``"png"`` (default) or ``"pdf"`` for vector output; the movie is
    always a gif). Cutouts are written to a temporary directory and
    discarded -- only those four files are kept. ``movie=False`` skips
    rendering/writing ``movie.gif`` entirely (it's by far the largest
    artifact -- see #32/#33's storage estimate -- and no other artifact
    depends on it: the PDF never embeds it, and the page just omits that
    column). Returns the filled-in ``VizSummary`` (with a
    ``sky_plot_data`` attribute -- ``(tracklet, gathered,
    exposure_times)`` -- stashed on it so a combined report can redraw
    the sky plot in vector form without re-measuring), or ``None`` if
    ``run_id``/``result_num`` doesn't exist (run `ingest searches`/
    `ingest images`/`search run` first)."""
    from pathlib import Path

    out_dir = Path(out_dir)
    resolved = resolve_result(session, run_id, result_num)
    if resolved is None:
        return None
    sd, tracklet, gathered = resolved
    exposures = touched_exposures(sd, tracklet)

    with tempfile.TemporaryDirectory(prefix="viz-result-") as cutout_dir:
        summary = measure_result(session, engine, sd, tracklet, exposures, product_type=product_type,
                                 cutout_dir=cutout_dir, cutout_size=cutout_size)
        exposure_times = [float(utc_to_tai(e.midpoint_mjd)) for e in exposures]
        for kind, path in (
            ("light_curve", render_light_curve_plot(summary, out_dir, fmt=plot_format)),
            ("coadd", render_coadd_plot(summary, out_dir, fmt=plot_format)),
            ("movie", render_movie(summary, out_dir, annotate=annotate) if movie else None),
            ("sky", render_sky_plot(run_id, result_num, tracklet, gathered, exposure_times, out_dir,
                                    fmt=plot_format)),
        ):
            if path is not None:
                summary.artifacts[kind] = str(path)
    summary.sky_plot_data = (tracklet, gathered, exposure_times)
    return summary
