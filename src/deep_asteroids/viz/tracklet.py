"""
Cutouts, forced photometry, a light-curve plot, a coadd and a movie for
one or more known objects -- given a ``known_tracklet`` row (#6's
tracklets ingest), not a search result (no ``Result`` exists yet in
deep-asteroids -- see issue #5's own "Not in scope" note).

Generalizes salad's ``mpc_viz.py`` (``render_tracklet_source_light_curve_plot()``/
``render_tracklet_source_coadd_plots()``/``render_tracklet_source_movie()``),
which does the same thing for a non-search ``TrackletSource``, but computes
+ renders directly in one pass rather than reading pre-populated
``Photometry``/``Cutout`` rows from a queue worker -- there's no queue here
(migration plan ground rule 3), so this measures forced photometry and
writes cutouts itself, driven by the CLI command, then renders from
those results immediately.

For every exposure the tracklet's own deep-db points touch on its night
(**not** the whole night's pointing group -- just where this object was
actually observed/predicted), the position comes from the tracklet's own
linear fit (``ingest.fit.predict()``), not the raw point -- this also
exercises the fit. A binary's ``barycenter``/``photocenter`` components
have no ``deep_db_synthetic_object_id`` of their own; their touched
exposures are the same ones their ``primary`` touches (computed per
exposure from the paired primary/companion positions at ingest time).
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, text

from deep_asteroids.database.images import Image
from deep_asteroids.database.tracklets import KnownTracklet
from deep_asteroids.ingest.fit import predict, utc_to_tai

log = logging.getLogger(__name__)

DEFAULT_PRODUCT_TYPE = "differenceExp"
DEFAULT_CUTOUT_SIZE = 51
_MOVIE_SCALE = 4  # nearest-neighbor upscale factor, matching salad's mpc_viz.py
_MOVIE_FRAME_DURATION_MS = 500  # 2 fps

_SYNTHETIC_POINTS = """
SELECT s.detector_exposure_id, e.midpoint_mjd
FROM synthetic_object_ephemeris s
JOIN exposure e ON e.id = s.exposure_id
JOIN night n ON n.id = e.night_id
WHERE s.synthetic_object_id = :object_id AND n.night = :night AND s.detector_exposure_id IS NOT NULL
"""

_SYNTHETIC_PRIMARY_ID = """
SELECT id FROM synthetic_object WHERE orbit_id = :orbit_id AND source = :source AND component = 'primary'
"""

_MPC_OBSERVATION_POINTS = """
SELECT o.detector_exposure_id, e.midpoint_mjd
FROM mpc_observation o
JOIN exposure e ON e.id = o.exposure_id
JOIN night n ON n.id = e.night_id
WHERE o.mpc_tracklet_id = :tracklet_id AND n.night = :night AND o.detector_exposure_id IS NOT NULL
"""

_MPCHECKER_POINTS = """
SELECT m.detector_exposure_id, e.midpoint_mjd
FROM mpchecker_ephemeris m
JOIN exposure e ON e.id = m.exposure_id
JOIN night n ON n.id = e.night_id
WHERE m.object_id = :object_id AND n.night = :night AND m.detector_exposure_id IS NOT NULL
"""


@dataclass
class TrackletMeasurement:
    """One exposure's worth of forced photometry + (optionally) a cutout
    for one ``known_tracklet``, at the exposure's own epoch."""
    deep_db_detector_exposure_id: int
    mjd_tai: float
    ra: float
    dec: float
    in_bounds: bool
    mag: float | None = None
    mag_err_lo: float | None = None
    mag_err_hi: float | None = None
    cutout_path: str | None = None


@dataclass
class VizSummary:
    tracklet: KnownTracklet
    n_exposures_touched: int = 0
    n_images_found: int = 0
    measurements: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)  # kind -> path

    def label(self):
        t = self.tracklet
        bits = [t.source, str(t.night)]
        if t.component:
            bits.append(t.component)
        if t.orbit_id is not None:
            bits.append(f"orbit_id={t.orbit_id}")
        if t.trkid:
            bits.append(f"trkid={t.trkid}")
        if t.provid:
            bits.append(t.provid)
        return " ".join(bits)

    def format(self):
        lines = [
            (f"{self.label()}: {self.n_exposures_touched} exposure(s) touched, "
            f"{self.n_images_found} image(s) found, {len(self.measurements)} measurement(s)"),
        ]
        for kind, path in sorted(self.artifacts.items()):
            lines.append(f"  {kind}: {path}")
        return "\n".join(lines)


def resolve_tracklets(session, source, night, trkid=None, orbit_id=None, component=None, provid=None,
                      permid=None):
    """``known_tracklet`` rows matching the given identity. A synthetic
    object with no ``component`` matches every component present (e.g.
    all four of a binary's own rows)."""
    q = select(KnownTracklet).where(KnownTracklet.source == source, KnownTracklet.night == night)
    if trkid is not None:
        q = q.where(KnownTracklet.trkid == trkid)
    if orbit_id is not None:
        q = q.where(KnownTracklet.orbit_id == orbit_id)
    if component is not None:
        q = q.where(KnownTracklet.component == component)
    if provid is not None:
        q = q.where(KnownTracklet.provid == provid)
    if permid is not None:
        q = q.where(KnownTracklet.permid == permid)
    return session.scalars(q.order_by(KnownTracklet.component)).all()


def touched_points(deep_engine, tracklet):
    """``[(deep_db_detector_exposure_id, midpoint_mjd_utc), ...]`` for
    every exposure ``tracklet``'s own deep-db points touch on its night."""
    with deep_engine.connect() as conn:
        if tracklet.source == "synthetic":
            object_id = tracklet.deep_db_synthetic_object_id
            if object_id is None:  # barycenter/photocenter: no object of their own -- use the primary's
                object_id = conn.execute(text(_SYNTHETIC_PRIMARY_ID), {
                    "orbit_id": tracklet.orbit_id, "source": tracklet.synthetic_source,
                }).scalar_one_or_none()
                if object_id is None:
                    return []
            rows = conn.execute(text(_SYNTHETIC_POINTS), {"object_id": object_id, "night": tracklet.night}).all()
        elif tracklet.source == "mpc_observation":
            rows = conn.execute(text(_MPC_OBSERVATION_POINTS), {
                "tracklet_id": tracklet.deep_db_mpc_tracklet_id, "night": tracklet.night,
            }).all()
        elif tracklet.source == "mpchecker":
            rows = conn.execute(text(_MPCHECKER_POINTS), {
                "object_id": tracklet.deep_db_mpc_object_id, "night": tracklet.night,
            }).all()
        else:
            raise ValueError(f"unknown source {tracklet.source!r}")
    return [(int(detector_exposure_id), float(midpoint_mjd)) for detector_exposure_id, midpoint_mjd in rows]


def measure_tracklet(session, deep_engine, engine, tracklet, product_type=DEFAULT_PRODUCT_TYPE,
                     cutout_dir=None, cutout_size=DEFAULT_CUTOUT_SIZE):
    """Forced photometry (+ a cutout, if ``cutout_dir`` is given) at
    ``tracklet``'s own predicted position on every exposure it touches.
    Returns a ``VizSummary`` with ``measurements`` filled in (no plots
    yet -- see ``render_*`` below)."""
    if tracklet.epoch is None:
        log.warning("%s has no fit (flag=%s) -- nothing to measure", tracklet, tracklet.flag)
        return VizSummary(tracklet=tracklet)

    points = sorted(touched_points(deep_engine, tracklet), key=lambda p: p[1])  # chronological (midpoint_mjd_utc)
    summary = VizSummary(tracklet=tracklet, n_exposures_touched=len(points))
    if not points:
        return summary

    detector_exposure_ids = [p[0] for p in points]
    images_by_detector_exposure = {
        img.deep_db_detector_exposure_id: img
        for img in session.scalars(
            select(Image).where(
                Image.deep_db_detector_exposure_id.in_(detector_exposure_ids), Image.product_type == product_type,
            )
        )
    }
    summary.n_images_found = len(images_by_detector_exposure)

    for i, (detector_exposure_id, midpoint_mjd_utc) in enumerate(points):
        image = images_by_detector_exposure.get(detector_exposure_id)
        if image is None:
            continue
        mjd_tai = float(utc_to_tai(midpoint_mjd_utc))
        ra, dec = predict(tracklet.epoch, tracklet.ra_epoch, tracklet.dec_epoch, tracklet.vra, tracklet.vdec, mjd_tai)
        ra, dec = float(ra), float(dec)

        from deep_asteroids.util.cutout import PhotometryRequest

        # the request id becomes the cutout's filename (<id>.fits) -- a
        # zero-padded chronological index prefix means a plain directory
        # listing sorts the same way the light curve/movie do, not by
        # deep_db_detector_exposure_id (which has no guaranteed relation
        # to time: ingest order, not observation order)
        request_id = f"{i:04d}_{detector_exposure_id}"
        (record,) = engine.measure(
            image.uri, [PhotometryRequest(id=request_id, ra=ra, dec=dec)],
            cutout_dir=cutout_dir, cutout_size=cutout_size,
        )
        summary.measurements.append(TrackletMeasurement(
            deep_db_detector_exposure_id=detector_exposure_id, mjd_tai=mjd_tai, ra=ra, dec=dec,
            in_bounds=record.in_bounds, mag=record.mag, mag_err_lo=record.mag_err_lo,
            mag_err_hi=record.mag_err_hi, cutout_path=record.cutout_path,
        ))
    # already chronological (points was pre-sorted above) -- re-asserted
    # explicitly so this invariant doesn't silently depend on that order
    summary.measurements.sort(key=lambda m: m.mjd_tai)
    return summary


def render_light_curve_plot(summary, out_dir):
    """``light_curve.png``: mag vs. MJD TAI, for every in-bounds
    measurement with a real (non-NaN) mag. Returns the path, or ``None``
    if there's nothing to plot."""
    import matplotlib
    matplotlib.use("Agg")  # headless cluster nodes -- never a display
    import matplotlib.pyplot as plt

    rows = [m for m in summary.measurements if m.in_bounds and m.mag is not None]
    if not rows:
        return None

    fig, ax = plt.subplots(figsize=(7, 5), dpi=110)
    t = [m.mjd_tai for m in rows]
    mag = [m.mag for m in rows]
    yerr_lo = [m.mag_err_lo or 0.0 for m in rows]
    yerr_hi = [m.mag_err_hi or 0.0 for m in rows]
    ax.errorbar(t, mag, yerr=[yerr_lo, yerr_hi], fmt="o", color="C0")
    ax.invert_yaxis()
    ax.set_xlabel("MJD TAI")
    ax.set_ylabel("mag")
    ax.set_title(f"{summary.label()}: light curve")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "light_curve.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def _cutout_stack(summary):
    """[(mjd_tai, 2D array), ...], chronological, for every measurement
    with a cutout that still exists on disk -- shared by
    render_coadd_plot()/render_movie()."""
    from pathlib import Path as _Path

    from astropy.io import fits

    frames = []
    for m in summary.measurements:
        if not m.cutout_path:
            continue
        if not _Path(m.cutout_path).exists():
            log.warning("cutout %s no longer exists, skipping", m.cutout_path)
            continue
        with fits.open(m.cutout_path) as hdul:
            frames.append((m.mjd_tai, hdul["IMAGE"].data.astype(float)))
    # measure_tracklet() already hands back measurements in mjd order, but
    # render_movie()'s frame order is exactly what a viewer judges "sorted
    # or not" by, so this doesn't trust that upstream invariant silently --
    # matches salad's own mpc_viz.py, which re-sorts here too.
    frames.sort(key=lambda f: f[0])
    return frames


def _zscale_limits(data):
    """``ZScaleInterval().get_limits()``, guarded against a degenerate
    (near-constant) image: its sample-based algorithm can come back with
    ``vmin >= vmax`` there, which `imshow`/`Normalize` reject outright.
    Falls back to the data's own min/max (widened by an epsilon if even
    those are equal)."""
    import numpy as np
    from astropy.visualization import ZScaleInterval

    vmin, vmax = ZScaleInterval().get_limits(data)
    if vmin < vmax:
        return vmin, vmax
    finite = data[np.isfinite(data)]
    vmin, vmax = float(finite.min()), float(finite.max())
    return (vmin, vmax) if vmin < vmax else (vmin - 0.5, vmax + 0.5)


def render_coadd_plot(summary, out_dir):
    """``coadd.png``: the median-stacked cutout, ZScale-stretched.
    Returns the path, or ``None`` if there are no cutouts to stack."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    frames = _cutout_stack(summary)
    if not frames:
        return None
    stack = np.stack([f[1] for f in frames])
    coadd = np.median(stack, axis=0)
    if not np.isfinite(coadd).any():
        log.warning("%s: coadd has no finite pixels, skipping", summary.label())
        return None

    vmin, vmax = _zscale_limits(coadd)
    fig, ax = plt.subplots(figsize=(5, 5), dpi=110)
    ax.imshow(coadd, cmap="gray", vmin=vmin, vmax=vmax, origin="lower")
    ax.set_title(f"{summary.label()}: coadd ({len(frames)} image(s))")
    ax.axis("off")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "coadd.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def render_movie(summary, out_dir):
    """``movie.gif``: one frame per cutout, chronological, shared
    ZScale normalization, 4x nearest-neighbor upscale, 2 fps (matching
    salad's mpc_viz.py). Returns the path, or ``None`` if there are no
    cutouts."""
    import numpy as np
    from PIL import Image as PILImage

    frames = _cutout_stack(summary)
    if not frames:
        return None
    stack = np.stack([f[1] for f in frames])
    if not np.isfinite(stack).any():
        log.warning("%s: frame stack has no finite pixels, skipping movie", summary.label())
        return None

    vmin, vmax = _zscale_limits(stack)
    span = max(vmax - vmin, 1e-12)

    pil_frames = []
    for _, data in frames:
        norm = np.clip((data - vmin) / span, 0.0, 1.0)
        gray = (np.flipud(norm) * 255).astype(np.uint8)  # match imshow(origin="lower")
        frame = PILImage.fromarray(gray, mode="L")
        frame = frame.resize((frame.width * _MOVIE_SCALE, frame.height * _MOVIE_SCALE), resample=PILImage.NEAREST)
        pil_frames.append(frame)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "movie.gif"
    pil_frames[0].save(path, save_all=True, append_images=pil_frames[1:], duration=_MOVIE_FRAME_DURATION_MS, loop=0)
    return path


def visualize_tracklet(session, deep_engine, engine, tracklet, out_dir, product_type=DEFAULT_PRODUCT_TYPE,
                       cutout_size=DEFAULT_CUTOUT_SIZE):
    """Measure + render everything for one ``known_tracklet``: cutouts
    under ``out_dir``, plus ``light_curve.png``/``coadd.png``/``movie.gif``.
    Returns the filled-in ``VizSummary``."""
    out_dir = Path(out_dir)
    summary = measure_tracklet(session, deep_engine, engine, tracklet, product_type=product_type,
                               cutout_dir=str(out_dir), cutout_size=cutout_size)
    for kind, render in (("light_curve", render_light_curve_plot), ("coadd", render_coadd_plot),
                         ("movie", render_movie)):
        path = render(summary, out_dir)
        if path is not None:
            summary.artifacts[kind] = str(path)
    return summary
