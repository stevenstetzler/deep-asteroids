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
from deep_asteroids.ingest.fit import predict as _predict_linear
from deep_asteroids.ingest.fit import sep_arcsec, utc_to_tai
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


def _seen(ra, dec, mjd_tai, gathered, radius_arcsec=2.0, time_tol_days=1e-4):
    """#38: is there a ``gathered`` (observed) point at this same exposure (``time`` within
    ``time_tol_days`` -- exposures are minutes to hours apart, so this only ever matches the
    one exposure this measurement is actually at) within ``radius_arcsec`` of the predicted
    position ``(ra, dec)``? I.e. did the search actually detect something consistent with the
    trajectory here, as opposed to this being a predicted-but-undetected (blank/noise)
    position."""
    return any(abs(g_time - mjd_tai) < time_tol_days and sep_arcsec(ra, dec, g_ra, g_dec) < radius_arcsec
               for g_ra, g_dec, g_time in gathered)


def measure_results(session, engine, jobs, product_type=DEFAULT_PRODUCT_TYPE, cutout_dir=None,
                    cutout_size=DEFAULT_CUTOUT_SIZE, workers=1):
    """``measure_result`` for several results at once: ``jobs`` is a list of ``(sd, tracklet, exposures,
    gathered)``, and the result is one ``VizSummary`` per job, in order.

    The cost of a measurement is reading the image, and the results of one search all fall on the same
    exposures, so every image is read **once**, with the predicted positions of every job that touches it
    in a single engine call, instead of once per result. ``workers`` > 1 overlaps that many image reads
    (the engine is a subprocess that waits on storage). Cutouts of all jobs go to the one ``cutout_dir``,
    each named by its request id (``<exposure index>_<detector-exposure id>_<job>``)."""
    from concurrent.futures import ThreadPoolExecutor

    summaries = []
    for sd, _tracklet, exposures, _gathered in jobs:
        identity = ResultIdentity(source="find-asteroids", night=sd.night, provid=f"{sd.run_id} result")
        summaries.append(VizSummary(tracklet=identity, n_exposures_touched=len(exposures)))

    detector_exposure_ids = {e.deep_db_detector_exposure_id for _sd, _t, exposures, _g in jobs for e in exposures}
    images = {}
    ids = sorted(detector_exposure_ids)
    for chunk in range(0, len(ids), 500):
        images.update({
            img.deep_db_detector_exposure_id: img
            for img in session.scalars(select(Image).where(
                Image.deep_db_detector_exposure_id.in_(ids[chunk:chunk + 500]), Image.product_type == product_type))})

    by_image = {}  # uri -> [(job, request id, exposure, image, mjd_tai, ra, dec)], one engine call per uri
    for j, (sd, tracklet, exposures, _gathered) in enumerate(jobs):
        summaries[j].n_images_found = len({e.deep_db_detector_exposure_id for e in exposures} & images.keys())
        for i, e in enumerate(exposures):
            image = images.get(e.deep_db_detector_exposure_id)
            if image is None:
                continue
            mjd_tai = float(utc_to_tai(e.midpoint_mjd))
            ra, dec = predict(tracklet, mjd_tai)
            request_id = f"{i:04d}_{e.deep_db_detector_exposure_id}_{j:03d}"
            by_image.setdefault(image.uri, []).append((j, request_id, e, image, mjd_tai, float(ra), float(dec)))

    def read_image(item):
        uri, entries = item
        requests = [PhotometryRequest(id=rid, ra=ra, dec=dec) for _j, rid, _e, _img, _t, ra, dec in entries]
        records = {r.id: r for r in engine.measure(uri, requests, cutout_dir=cutout_dir, cutout_size=cutout_size)}
        return entries, records

    if workers > 1 and len(by_image) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = list(pool.map(read_image, by_image.items()))
    else:
        done = [read_image(item) for item in by_image.items()]

    for entries, records in done:
        for j, rid, e, image, mjd_tai, ra, dec in entries:
            record = records[rid]
            summaries[j].measurements.append(TrackletMeasurement(
                deep_db_detector_exposure_id=e.deep_db_detector_exposure_id, mjd_tai=mjd_tai, ra=ra,
                dec=dec, in_bounds=record.in_bounds, night=image.night, field=image.field, expnum=image.expnum,
                mag=record.mag, mag_err_lo=record.mag_err_lo, mag_err_hi=record.mag_err_hi,
                cutout_path=record.cutout_path, seen=_seen(ra, dec, mjd_tai, jobs[j][3]),
            ))
    for summary in summaries:
        summary.measurements.sort(key=lambda m: m.mjd_tai)
        # a cutout clipped by a chip edge has a different shape and can't be stacked for the coadd/movie --
        # keep only the modal shape (matches examples/viz_result.py's original behavior)
        shapes = [(m, fits.getdata(m.cutout_path).shape) for m in summary.measurements if m.cutout_path]
        if shapes:
            modal = collections.Counter(shape for _, shape in shapes).most_common(1)[0][0]
            for m, shape in shapes:
                if shape != modal:
                    m.cutout_path = None
    return summaries


def measure_result(session, engine, sd, tracklet, exposures, gathered=(), product_type=DEFAULT_PRODUCT_TYPE,
                   cutout_dir=None, cutout_size=DEFAULT_CUTOUT_SIZE):
    """Forced photometry (+ a cutout, if ``cutout_dir`` is given) at
    ``tracklet``'s own predicted position on every one of ``exposures``.
    ``gathered`` (the result's own observed Hough-stage detections --
    ``resolve_result()``'s own return) tags each measurement's ``seen`` (#38): whether a
    gathered point within 2" of the predicted position exists at that exposure's own time.
    Returns a ``VizSummary`` with ``measurements`` filled in (no plots yet). One result at a
    time; ``measure_results`` does many, reading each image once."""
    (summary,) = measure_results(session, engine, [(sd, tracklet, exposures, gathered)], product_type=product_type,
                                 cutout_dir=cutout_dir, cutout_size=cutout_size)
    return summary


def render_sky_plot(run_id, result_num, tracklet, gathered, exposure_times, out_dir, ax=None, fmt="png",
                    title=True, matched=None):
    """The result's gathered (observed) detections colored by time, with
    its tracklet's predicted track as a translucent arrow over the
    searched time span, drawn on top of the detections (not a salad-style
    opaque arrow underneath plus an x marking the predicted position in
    every exposure -- the detections are the point, the arrow is context).
    By default creates and saves its own figure (``sky.<fmt>``), returning
    its path -- or ``None`` if there's nothing gathered to plot, or (with
    ``ax`` given) nothing was drawn. With ``ax`` given, draws directly onto
    it instead (the caller owns saving) and always returns ``None`` -- see
    ``viz/tracklet.py``'s ``render_light_curve_plot()``. ``title=False``
    skips the axes title.

    ``matched`` (#38), if given, is the ``known_tracklet`` row this result matches
    (``crossmatch.classify.matched_known_tracklet()``'s own return -- synthetic/mpchecker
    only) -- its own predicted track is drawn as a second, differently-colored arrow over
    the same time span, so the two fits (this result's own vs. the known object's) can be
    visually compared.

    Axes are RA/Dec offset from this plot's own minimum (in degrees), in arcmin -- a plain
    linear offset, not a sky projection (no cos(dec) factor, same convention ``vra`` already
    uses elsewhere in this codebase): over one result's own small span this is visually
    indistinguishable from a proper projection, and it keeps the exact reference point
    (printed in the axis labels) so the plot is still exactly reconstructible in degrees."""
    if not gathered:
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.lines import Line2D

    fig = None
    if ax is None:
        fig, ax = plt.subplots(figsize=(14, 6), dpi=110)
    ra, dec, time = (np.array(c) for c in zip(*gathered))
    t0 = float(time.min())

    tmin, tmax = min([*exposure_times, time.min()]), max([*exposure_times, time.max()])
    (ra0, dec0), (ra1, dec1) = predict(tracklet, tmin), predict(tracklet, tmax)

    matched_track = None
    if matched is not None:
        (mra0, mdec0) = _predict_linear(matched.epoch, matched.ra_epoch, matched.dec_epoch, matched.vra,
                                        matched.vdec, tmin)
        (mra1, mdec1) = _predict_linear(matched.epoch, matched.ra_epoch, matched.dec_epoch, matched.vra,
                                        matched.vdec, tmax)
        matched_track = (float(mra0), float(mdec0), float(mra1), float(mdec1))

    # the arcmin-offset reference point: this plot's own minimum RA/Dec (in degrees), over
    # every point that will actually be drawn. The predicted track is linear, so its two
    # endpoints (ra0/dec0, ra1/dec1) already bound its full extent -- no need for every
    # per-exposure point along it.
    all_ra = np.r_[ra, [ra0, ra1], ([matched_track[0], matched_track[2]] if matched_track else [])]
    all_dec = np.r_[dec, [dec0, dec1], ([matched_track[1], matched_track[3]] if matched_track else [])]
    ra_min, dec_min = float(all_ra.min()), float(all_dec.min())

    def arcmin(ra_deg, dec_deg):
        return (ra_deg - ra_min) * 60.0, (dec_deg - dec_min) * 60.0

    ra_am, dec_am = arcmin(ra, dec)
    sc = ax.scatter(ra_am, dec_am, c=time - t0, cmap="viridis", label="observed (gathered)", zorder=2)
    # fraction/pad tighter than matplotlib's own defaults (0.15/0.05): those default to eating
    # a chunk of whatever's to the right of a manually-placed Axes (e.g. the PDF report's
    # light-curve panel, immediately to this one's right with no room to spare), overlapping
    # its "mag" ylabel with this colorbar's own rotated label text. A compact one still reads
    # fine in the standalone sky.png (ax=None, no neighbor) too.
    cbar = ax.figure.colorbar(sc, ax=ax, fraction=0.05, pad=0.02)
    cbar.set_label(f"time since {t0:.5f} MJD TAI (day)", fontsize=9)
    cbar.ax.tick_params(labelsize=8)

    ra0_am, dec0_am = arcmin(ra0, dec0)
    ra1_am, dec1_am = arcmin(ra1, dec1)
    # zorder above the scatter's (2), and translucent: the line is context drawn over the
    # detections, not something the detections should be hidden underneath (#38 follow-up --
    # this replaces the earlier opaque-arrow-plus-per-exposure-x-marker treatment).
    # A plain Line2D, not FancyArrowPatch (tried first): with solid_capstyle="butt" it ends
    # exactly flush at (ra0,dec0)/(ra1,dec1), no shrink/mutation_scale bookkeeping to get
    # right, and -- verified side by side -- no visible difference in where it stops; a
    # stray high-residual detection at the tracklet's own end was being misread as "the line
    # stopping short of the last point" when the line was already reaching the real fit.
    # No `label=` here: a legend handle auto-built from this line would inherit its own
    # linewidth (16) and visibly overflow the legend's own handle box. A separate, thin
    # Line2D proxy below (same color/alpha/linestyle) stands in for it in the legend instead.
    ax.add_line(Line2D([ra0_am, ra1_am], [dec0_am, dec1_am], color="C0", alpha=0.35, linewidth=16,
                       zorder=3, solid_capstyle="butt"))
    legend_handles = [sc, Line2D([0], [0], color="C0", alpha=0.35, linewidth=3, label="predicted (tracklet)")]

    xs, ys = [ra_am, [ra0_am], [ra1_am]], [dec_am, [dec0_am], [dec1_am]]
    if matched_track is not None:
        mra0_am, mdec0_am = arcmin(matched_track[0], matched_track[1])
        mra1_am, mdec1_am = arcmin(matched_track[2], matched_track[3])
        ax.add_line(Line2D([mra0_am, mra1_am], [mdec0_am, mdec1_am], color="C1", alpha=0.35,
                           linewidth=16, zorder=3, solid_capstyle="butt", linestyle="--"))
        legend_handles.append(Line2D([0], [0], color="C1", alpha=0.35, linewidth=3, linestyle="--",
                                     label=f"matched {matched.source} track"))
        xs.append([mra0_am, mra1_am])
        ys.append([mdec0_am, mdec1_am])

    xs, ys = np.concatenate(xs), np.concatenate(ys)
    xpad, ypad = (xs.max() - xs.min()) * 0.08 or 1e-4, (ys.max() - ys.min()) * 0.08 or 1e-4
    ax.set_xlim(xs.min() - xpad, xs.max() + xpad)
    ax.set_ylim(ys.min() - ypad, ys.max() + ypad)
    ax.set_xlabel(f"RA - {ra_min:.5f} deg (arcmin)")
    ax.set_ylabel(f"Dec - {dec_min:.5f} deg (arcmin)")
    ax.grid(True, alpha=0.3)
    if title:
        ax.set_title(f"{run_id} #{result_num}: sky")
    ax.legend(handles=legend_handles, loc="best")

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
    discarded -- only those plots are kept. ``movie=False`` skips
    rendering/writing ``movie.gif`` entirely (it's by far the largest
    artifact -- see #32/#33's storage estimate -- and no other artifact
    depends on it: the PDF never embeds it, and the page just omits that
    column).

    Three coadds are rendered, not one (#38): ``coadd`` (every touched
    exposure with a surviving cutout, unchanged from before #38),
    ``coadd_seen`` (only the exposures where a ``gathered`` point within 2"
    of the predicted position exists -- the search actually detected
    something there) and ``coadd_unseen`` (the rest -- predicted but not
    detected). Either can be missing from ``summary.artifacts`` if that
    subset is empty. The sky plot also draws a matched synthetic/mpchecker
    object's own trajectory alongside this result's, if ``crossmatch`` found
    one within its usual radius (``crossmatch.classify.matched_known_tracklet()``).

    Returns the filled-in ``VizSummary`` (with a ``sky_plot_data`` attribute
    -- ``(tracklet, gathered, exposure_times, matched)`` -- stashed on it so
    a combined report can redraw the sky plot in vector form without
    re-measuring), or ``None`` if ``run_id``/``result_num`` doesn't exist
    (run `ingest searches`/`ingest images`/`search run` first)."""
    from pathlib import Path

    from deep_asteroids.crossmatch.classify import matched_known_tracklet

    out_dir = Path(out_dir)
    resolved = resolve_result(session, run_id, result_num)
    if resolved is None:
        return None
    sd, tracklet, gathered = resolved
    exposures = touched_exposures(sd, tracklet)
    matched = matched_known_tracklet(session, tracklet.result_id)

    with tempfile.TemporaryDirectory(prefix="viz-result-") as cutout_dir:
        summary = measure_result(session, engine, sd, tracklet, exposures, gathered=gathered,
                                 product_type=product_type, cutout_dir=cutout_dir, cutout_size=cutout_size)
        _render_result(summary, run_id, result_num, tracklet, gathered, exposures, matched, out_dir,
                       annotate=annotate, plot_format=plot_format, movie=movie)
    return summary


def _render_result(summary, run_id, result_num, tracklet, gathered, exposures, matched, out_dir, annotate=None,
                   plot_format="png", movie=True):
    """The plots of ``visualize_result`` from an already-measured ``summary`` (its cutouts must still exist)."""
    exposure_times = [float(utc_to_tai(e.midpoint_mjd)) for e in exposures]
    for kind, path in (
        ("light_curve", render_light_curve_plot(summary, out_dir, fmt=plot_format)),
        ("coadd", render_coadd_plot(summary, out_dir, fmt=plot_format)),
        ("coadd_seen", render_coadd_plot(summary, out_dir, fmt=plot_format, which="seen")),
        ("coadd_unseen", render_coadd_plot(summary, out_dir, fmt=plot_format, which="unseen")),
        ("movie", render_movie(summary, out_dir, annotate=annotate) if movie else None),
        ("sky", render_sky_plot(run_id, result_num, tracklet, gathered, exposure_times, out_dir,
                                fmt=plot_format, matched=matched)),
    ):
        if path is not None:
            summary.artifacts[kind] = str(path)
    summary.sky_plot_data = (tracklet, gathered, exposure_times, matched)


def visualize_results(session, engine, run_id, result_nums, out_dir_for, product_type=DEFAULT_PRODUCT_TYPE,
                      cutout_size=DEFAULT_CUTOUT_SIZE, annotate=None, plot_format="png", movie=True, workers=1,
                      batch_size=16):
    """``visualize_result`` for several results of one search: yields ``(result_num, out_dir, summary)`` for each
    of ``result_nums`` (``summary`` is ``None`` for one that does not exist), writing each result's plots to
    ``out_dir_for(result_num)``.

    The results of a search share its images, so they are measured together, ``batch_size`` at a time, each image
    read once per batch rather than once per result (``measure_results``; ``workers`` overlaps image reads). A
    batch's cutouts live in one temporary directory until its results are rendered, so ``batch_size`` bounds the
    disk and memory used."""
    from pathlib import Path

    from deep_asteroids.crossmatch.classify import matched_known_tracklet

    result_nums = list(result_nums)
    for start in range(0, len(result_nums), batch_size):
        batch = result_nums[start:start + batch_size]
        resolved = {n: resolve_result(session, run_id, n) for n in batch}
        present = [n for n in batch if resolved[n] is not None]
        for n in batch:
            if resolved[n] is None:
                yield n, Path(out_dir_for(n)), None
        if not present:
            continue
        exposures = {n: touched_exposures(resolved[n][0], resolved[n][1]) for n in present}
        with tempfile.TemporaryDirectory(prefix="viz-result-") as cutout_dir:
            summaries = measure_results(
                session, engine, [(resolved[n][0], resolved[n][1], exposures[n], resolved[n][2]) for n in present],
                product_type=product_type, cutout_dir=cutout_dir, cutout_size=cutout_size, workers=workers)
            for n, summary in zip(present, summaries):
                sd, tracklet, gathered = resolved[n]
                out_dir = Path(out_dir_for(n))
                _render_result(summary, run_id, n, tracklet, gathered, exposures[n],
                               matched_known_tracklet(session, tracklet.result_id), out_dir,
                               annotate=annotate, plot_format=plot_format, movie=movie)
                yield n, out_dir, summary
