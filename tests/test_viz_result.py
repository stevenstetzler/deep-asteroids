"""Tests for viz/result.py and `deep-asteroids viz result`: promotes
examples/viz_result.py into a reusable CLI primitive (#27)."""
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from click.testing import CliRunner
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure
from deep_asteroids.search.run import run_search_for_definition
from deep_asteroids.util.cutout import CutoutPhotometryEngine, PhotometryRecord
from deep_asteroids.viz.result import resolve_result, touched_exposures, visualize_result

NIGHT = 20190403
RUN_ID = "deep-decam-T1-ccd1-20190403"
T0 = 58577.2
CADENCE = 0.0104  # ~15 minutes
N_EXPOSURES = 12
MOVER = {"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.1}
SEARCH = {"velocity": (0.1, 0.5), "num_results": 3, "dx": 10.0}


class FakeCutoutEngine(CutoutPhotometryEngine):
    """Same convention as test_viz_tracklet.py's: ra < 0 is out-of-bounds;
    otherwise a FITS cutout (if cutout_dir is given) and a mag that varies
    by request id."""

    def measure(self, uri, requests, cutout_dir=None, cutout_size=51):
        for r in requests:
            in_bounds = r.ra >= 0
            mag = 20.0 + (int(r.id.split("_")[0]) % 5) * 0.1 if in_bounds else None
            cutout_path = None
            if in_bounds and cutout_dir is not None:
                Path(cutout_dir).mkdir(parents=True, exist_ok=True)
                data = np.full((cutout_size, cutout_size), float(int(r.id.split("_")[0]) % 7))
                hdul = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(data=data, name="IMAGE")])
                cutout_path = str(Path(cutout_dir) / f"{r.id}.fits")
                hdul.writeto(cutout_path, overwrite=True)
            yield PhotometryRecord(id=r.id, in_bounds=in_bounds, ra=r.ra, dec=r.dec, mag=mag,
                                   mag_err_lo=0.01, mag_err_hi=0.02, cutout_path=cutout_path)


class StubDetector:
    """Returns the planted mover (plus noise) for the exposure a uri names (matches test_search.py)."""
    threshold, polarity = 5.0, "positive"

    def __init__(self, exposure_times, n_noise=20):
        self.times, self.n_noise = exposure_times, n_noise

    def detect(self, uri):
        from astropy.table import Table

        i = int(uri.rsplit("_", 1)[1].removesuffix(".fits"))
        rng = np.random.default_rng(1000 + i)
        dt = self.times[i] - self.times[0]
        jitter = rng.normal(0, 5e-5, 2)
        ra = np.r_[MOVER["ra"] + MOVER["vra"] * dt + jitter[0], 215.0 + rng.uniform(-0.05, 0.05, self.n_noise)]
        dec = np.r_[MOVER["dec"] + MOVER["vdec"] * dt + jitter[1], -13.0 + rng.uniform(-0.05, 0.05, self.n_noise)]
        return Table({"ra": ra, "dec": dec, "flux": np.full(len(ra), 100.0), "mag": np.full(len(ra), 22.0)})


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


@pytest.fixture
def definition(session):
    times = [T0 + i * CADENCE for i in range(N_EXPOSURES)]
    sd = SearchDefinition(
        night=NIGHT, field="T1", detector_number=1, detector_name="S1", run_id=RUN_ID, band=None,
        n_exposures=N_EXPOSURES, t_start_mjd=times[0], t_end_mjd=times[-1], created_at=datetime(2026, 1, 1),
    )
    for i, t in enumerate(times):
        sd.exposures.append(SearchDefinitionExposure(
            deep_db_exposure_id=100 + i, deep_db_detector_exposure_id=1000 + i, expnum=5000 + i, midpoint_mjd=t))
        session.add(Image(
            night=NIGHT, field="T1", detector_number=1, expnum=5000 + i, deep_db_exposure_id=100 + i,
            deep_db_detector_exposure_id=1000 + i, product_type="differenceExp", uri=f"file:///fake/exp_{i}.fits",
            created_at=datetime(2026, 1, 1)))
    session.add(sd)
    session.commit()
    return sd


@pytest.fixture
def result_num(session, definition, tmp_path):
    """Runs a real search (a planted mover against a stub detector) so there's a genuine
    Result/Tracklet/Gathered row to visualize; returns its result_num (the best one, 0)."""
    detector = StubDetector([e.midpoint_mjd for e in definition.exposures])
    summary = run_search_for_definition(session, definition, tmp_path / "cat", detector, workers=2, **SEARCH)
    assert summary.ran and summary.n_results >= 1
    return 0


def test_resolve_result_and_touched_exposures(session, definition, result_num):
    resolved = resolve_result(session, RUN_ID, result_num)
    assert resolved is not None
    sd, tracklet, gathered = resolved
    assert sd.run_id == RUN_ID and tracklet.result_id is not None and len(gathered) >= N_EXPOSURES - 1

    exposures = touched_exposures(sd, tracklet)
    assert len(exposures) >= N_EXPOSURES - 1
    assert exposures == sorted(exposures, key=lambda e: e.midpoint_mjd)


def test_resolve_result_missing(session, definition, result_num):
    assert resolve_result(session, RUN_ID, 999) is None
    assert resolve_result(session, "no-such-run", 0) is None


def test_visualize_result_renders_all_four_plots(session, definition, result_num, tmp_path):
    out_dir = tmp_path / "out"
    summary = visualize_result(session, FakeCutoutEngine(), RUN_ID, result_num, out_dir)

    assert summary is not None
    assert summary.n_exposures_touched >= N_EXPOSURES - 1
    assert summary.n_images_found == summary.n_exposures_touched  # every touched exposure has an Image row
    assert set(summary.artifacts) == {"light_curve", "coadd", "movie", "sky"}
    for path in summary.artifacts.values():
        assert Path(path).exists()
    assert f"{RUN_ID} result" in summary.format()

    # cutouts are discarded -- only the four plots remain in out_dir
    assert sorted(p.name for p in out_dir.iterdir()) == ["coadd.png", "light_curve.png", "movie.gif", "sky.png"]


def test_visualize_result_missing_returns_none(session, definition, tmp_path):
    assert visualize_result(session, FakeCutoutEngine(), RUN_ID, 999, tmp_path / "out") is None


def test_cli_viz_result(session, definition, result_num, tmp_path, monkeypatch):
    monkeypatch.setattr("deep_asteroids.util.cutout.LSSTCutoutPhotometry", FakeCutoutEngine)
    out_dir = tmp_path / "cli_out"
    runner = CliRunner()
    result = runner.invoke(main, [
        "viz", "result", "--run-id", RUN_ID, "--result-num", str(result_num), "--out-dir", str(out_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "image(s) found" in result.output
    assert (out_dir / "sky.png").exists() and (out_dir / "movie.gif").exists()

    missing = runner.invoke(main, [
        "viz", "result", "--run-id", RUN_ID, "--result-num", "999", "--out-dir", str(out_dir),
    ])
    assert missing.exit_code != 0 and "no result 999" in missing.output
