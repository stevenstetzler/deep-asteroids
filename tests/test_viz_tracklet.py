from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from sqlalchemy import text
from sqlalchemy.orm import Session

from deep_asteroids.database import create_db, get_deep_db
from deep_asteroids.database.images import Image
from deep_asteroids.ingest.tracklets import ingest_source
from deep_asteroids.util.cutout import CutoutPhotometryEngine, PhotometryRecord
from deep_asteroids.viz.tracklet import (
    _annotation_text,
    _cutout_stack,
    _fit_annotation,
    measure_tracklet,
    render_coadd_plot,
    render_light_curve_plot,
    render_movie,
    resolve_tracklets,
    touched_points,
    visualize_tracklet,
)

NIGHT = 20190402


class FakeCutoutEngine(CutoutPhotometryEngine):
    """Deterministic stand-in for LSSTCutoutPhotometry: ra < 0 is the
    out-of-bounds sentinel (matching test_cutout.py's convention);
    everything else gets a FITS cutout of the requested cutout_size
    written if cutout_dir is given, and a mag that varies by the request
    id so a light curve has something to plot."""

    def measure(self, uri, requests, cutout_dir=None, cutout_size=51):
        for r in requests:
            in_bounds = r.ra >= 0
            mag = 20.0 + (int(r.id) % 5) * 0.1 if in_bounds else None
            cutout_path = None
            if in_bounds and cutout_dir is not None:
                Path(cutout_dir).mkdir(parents=True, exist_ok=True)
                data = np.full((cutout_size, cutout_size), float(int(r.id) % 7))
                hdul = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(data=data, name="IMAGE")])
                cutout_path = str(Path(cutout_dir) / f"{r.id}.fits")
                hdul.writeto(cutout_path, overwrite=True)
            yield PhotometryRecord(id=r.id, in_bounds=in_bounds, ra=r.ra, dec=r.dec, mag=mag,
                                   mag_err_lo=0.01, mag_err_hi=0.02, cutout_path=cutout_path)


@pytest.fixture
def session(db_url, deep_db_url):
    with Session(create_db()) as session:
        yield session


def _detector_exposure_ids(detector_number):
    with get_deep_db().connect() as conn:
        rows = conn.execute(text(
            "SELECT de.id FROM detector_exposure de JOIN detector d ON d.id = de.detector_id "
            "WHERE d.number = :n ORDER BY de.id"
        ), {"n": detector_number}).all()
    return [r[0] for r in rows]


@pytest.fixture
def images(session):
    """One Image row per detector-4/detector-5 exposure in the fixture
    deep-db, so measure_tracklet() has something to look up."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for detector_number in (4, 5):
        for i, detector_exposure_id in enumerate(_detector_exposure_ids(detector_number)):
            rows.append(Image(
                night=NIGHT, field="A0b", detector_number=detector_number, expnum=845580 + i,
                deep_db_exposure_id=i + 1, deep_db_detector_exposure_id=detector_exposure_id,
                product_type="differenceExp", uri=f"file:///fake/{detector_number}_{i}.fits", created_at=now,
            ))
    session.add_all(rows)
    session.commit()
    return rows


@pytest.fixture
def known_tracklets(session):
    for source in ("synthetic", "mpc_observation", "mpchecker"):
        ingest_source(session, get_deep_db(), source)
    return session


def test_resolve_tracklets_by_trkid(known_tracklets):
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    assert t.trkid == "00000E2QG2"


def test_resolve_tracklets_by_provid(known_tracklets):
    (t,) = resolve_tracklets(known_tracklets, "mpchecker", NIGHT, provid="2019 GA179")
    assert t.provid == "2019 GA179"


def test_resolve_tracklets_binary_without_component_gets_all_four(known_tracklets):
    rows = resolve_tracklets(known_tracklets, "synthetic", NIGHT, orbit_id=1002)
    assert {r.component for r in rows} == {"primary", "companion", "barycenter", "photocenter"}


def test_resolve_tracklets_binary_with_component(known_tracklets):
    (t,) = resolve_tracklets(known_tracklets, "synthetic", NIGHT, orbit_id=1002, component="barycenter")
    assert t.component == "barycenter"


def test_touched_points_synthetic_primary(known_tracklets):
    (t,) = resolve_tracklets(known_tracklets, "synthetic", NIGHT, orbit_id=1001, component="primary")
    assert len(touched_points(get_deep_db(), t)) == 3


def test_touched_points_synthetic_barycenter_uses_primarys_exposures(known_tracklets):
    (t,) = resolve_tracklets(known_tracklets, "synthetic", NIGHT, orbit_id=1002, component="barycenter")
    points = touched_points(get_deep_db(), t)
    assert len(points) == 3
    primary_points = touched_points(get_deep_db(), resolve_tracklets(
        known_tracklets, "synthetic", NIGHT, orbit_id=1002, component="primary")[0])
    assert {p[0] for p in points} == {p[0] for p in primary_points}


def test_touched_points_mpchecker_excludes_off_chip(known_tracklets):
    """conftest's mpchecker fixture has the 3rd exposure off-chip for
    this object -- only 2 of 3 points have a detector_exposure_id."""
    (t,) = resolve_tracklets(known_tracklets, "mpchecker", NIGHT, provid="2019 GA179")
    assert len(touched_points(get_deep_db(), t)) == 2


def test_touched_points_mpc_observation(known_tracklets):
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    assert len(touched_points(get_deep_db(), t)) == 3


def test_measure_tracklet(known_tracklets, images, tmp_path):
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    summary = measure_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t, cutout_dir=str(tmp_path))
    assert summary.n_exposures_touched == 3
    assert summary.n_images_found == 3
    assert len(summary.measurements) == 3
    assert [m.mjd_tai for m in summary.measurements] == sorted(m.mjd_tai for m in summary.measurements)
    assert all(m.in_bounds for m in summary.measurements)
    assert all(m.cutout_path and Path(m.cutout_path).exists() for m in summary.measurements)

    # cutout filenames carry a zero-padded chronological index, so a plain
    # directory listing sorts the same way the light curve/movie do --
    # deep_db_detector_exposure_id alone has no guaranteed relation to time
    filenames = [Path(m.cutout_path).name for m in summary.measurements]
    assert filenames == sorted(filenames)
    assert [f.split("_", 1)[0] for f in filenames] == ["0000", "0001", "0002"]

    # night/field/expnum come from the matched Image row -- --annotate reads these
    assert all(m.night == NIGHT and m.field == "A0b" for m in summary.measurements)
    assert sorted(m.expnum for m in summary.measurements) == [845580, 845581, 845582]


def test_annotation_text_formats_measurement_fields(known_tracklets, images, tmp_path):
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    summary = measure_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t, cutout_dir=str(tmp_path))
    m = summary.measurements[0]
    assert _annotation_text("{night}/{field}/{expnum}", m) == f"{NIGHT}/A0b/{m.expnum}"
    assert _annotation_text("mag={mag:.2f}", m) == f"mag={m.mag:.2f}"


def test_annotation_text_rejects_unknown_field(known_tracklets, images, tmp_path):
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    summary = measure_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t, cutout_dir=str(tmp_path))
    with pytest.raises(ValueError, match="unknown field"):
        _annotation_text("{nope}", summary.measurements[0])


def test_fit_annotation_shrinks_and_truncates_to_fit_width():
    from PIL import Image as PILImage
    from PIL import ImageDraw

    draw = ImageDraw.Draw(PILImage.new("L", (1, 1)))
    long_text = "20190402/A0b/845580 this is a very long annotation string indeed"

    font, text = _fit_annotation(draw, long_text, max_width=200, max_size=40)
    assert draw.textlength(text, font=font) <= 200

    # even the smallest font doesn't fit a string this long in this little
    # width -- falls back to truncating with an ellipsis, still fitting
    font, text = _fit_annotation(draw, long_text, max_width=40, max_size=40, min_size=10)
    assert draw.textlength(text, font=font) <= 40
    assert text.endswith("…")


def test_render_movie_with_annotate_draws_white_text_that_fits(known_tracklets, images, tmp_path):
    from PIL import Image as PILImage

    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    summary = measure_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t, cutout_dir=str(tmp_path))

    plain_path = render_movie(summary, tmp_path / "plain")
    annotated_path = render_movie(summary, tmp_path / "annotated", annotate="{night}/{field}/{expnum}")
    assert plain_path is not None and annotated_path is not None

    plain = PILImage.open(plain_path)
    annotated = PILImage.open(annotated_path)
    assert plain.size == annotated.size  # annotation doesn't resize the frame

    # the annotated frame must have some pure-white (255) pixels near the
    # top that the plain one doesn't -- i.e. text was actually drawn, in
    # white, within the frame's own width (no wraparound/clipping)
    plain_arr = np.array(plain.convert("L"))
    annotated_arr = np.array(annotated.convert("L"))
    top_band = slice(0, 20)
    assert not (plain_arr[top_band] == 255).any()
    assert (annotated_arr[top_band] == 255).any()
    white_columns = np.flatnonzero((annotated_arr[top_band] == 255).any(axis=0))
    assert white_columns.min() >= 0
    assert white_columns.max() < annotated_arr.shape[1]


def test_viz_tracklet_annotate_rejects_unknown_field_via_cli(known_tracklets, images, tmp_path):
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    with pytest.raises(ValueError, match="unknown field"):
        visualize_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t, tmp_path, annotate="{nope}")


def test_render_movie_sorts_frames_even_if_measurements_are_not(known_tracklets, images, tmp_path):
    """_cutout_stack()/render_movie() re-sort defensively -- this doesn't
    rely on measure_tracklet() having already sorted its own output."""
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    summary = measure_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t, cutout_dir=str(tmp_path))
    assert len(summary.measurements) == 3
    summary.measurements.reverse()  # deliberately scramble the already-correct order
    assert render_movie(summary, tmp_path) is not None

    frames = _cutout_stack(summary)
    mjds = [m.mjd_tai for m, _ in frames]
    assert mjds == sorted(mjds)


def test_measure_tracklet_no_fit_returns_empty_summary(known_tracklets, images):
    """A too_few_points/conflated/same_obstime_conflict row has no fit --
    nothing to measure, not an error."""
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000SING1")
    assert t.flag == "too_few_points"
    summary = measure_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t)
    assert summary.measurements == []


def test_visualize_tracklet_writes_all_artifacts(known_tracklets, images, tmp_path):
    (t,) = resolve_tracklets(known_tracklets, "mpchecker", NIGHT, provid="2019 GA179")
    summary = visualize_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t, tmp_path)
    assert set(summary.artifacts) == {"light_curve", "coadd", "movie"}
    for path in summary.artifacts.values():
        assert Path(path).exists() and Path(path).stat().st_size > 0


def test_render_functions_return_none_with_no_measurements(known_tracklets, tmp_path):
    (t,) = resolve_tracklets(known_tracklets, "mpchecker", NIGHT, provid="2019 GA179")
    from deep_asteroids.viz.tracklet import VizSummary

    empty = VizSummary(tracklet=t)
    assert render_light_curve_plot(empty, tmp_path) is None
    assert render_coadd_plot(empty, tmp_path) is None
    assert render_movie(empty, tmp_path) is None
