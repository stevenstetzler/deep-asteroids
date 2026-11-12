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
    _cutout_stack,
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
    everything else gets a small FITS cutout written if cutout_dir is
    given, and a mag that varies by the request id so a light curve has
    something to plot."""

    def measure(self, uri, requests, cutout_dir=None, cutout_size=51):
        for r in requests:
            in_bounds = r.ra >= 0
            mag = 20.0 + (int(r.id) % 5) * 0.1 if in_bounds else None
            cutout_path = None
            if in_bounds and cutout_dir is not None:
                Path(cutout_dir).mkdir(parents=True, exist_ok=True)
                data = np.full((5, 5), float(int(r.id) % 7))
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


def test_render_movie_sorts_frames_even_if_measurements_are_not(known_tracklets, images, tmp_path):
    """_cutout_stack()/render_movie() re-sort defensively -- this doesn't
    rely on measure_tracklet() having already sorted its own output."""
    (t,) = resolve_tracklets(known_tracklets, "mpc_observation", NIGHT, trkid="00000E2QG2")
    summary = measure_tracklet(known_tracklets, get_deep_db(), FakeCutoutEngine(), t, cutout_dir=str(tmp_path))
    assert len(summary.measurements) == 3
    summary.measurements.reverse()  # deliberately scramble the already-correct order
    assert render_movie(summary, tmp_path) is not None

    frames = _cutout_stack(summary)
    assert [f[0] for f in frames] == sorted(f[0] for f in frames)


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
