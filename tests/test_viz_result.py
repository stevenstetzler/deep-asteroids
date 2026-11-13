"""Tests for viz/result.py and `deep-asteroids viz result`: promotes
examples/viz_result.py into a reusable CLI primitive (#27)."""
from pathlib import Path

import pytest
import result_viz_helpers as h
from click.testing import CliRunner
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.viz.result import resolve_result, touched_exposures, visualize_result


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


@pytest.fixture
def definition(session):
    return h.build_definition(session)


@pytest.fixture
def result_num(session, definition, tmp_path):
    return h.run_planted_search(session, definition, tmp_path)


def test_resolve_result_and_touched_exposures(session, definition, result_num):
    resolved = resolve_result(session, h.RUN_ID, result_num)
    assert resolved is not None
    sd, tracklet, gathered = resolved
    assert sd.run_id == h.RUN_ID and tracklet.result_id is not None and len(gathered) >= h.N_EXPOSURES - 1

    exposures = touched_exposures(sd, tracklet)
    assert len(exposures) >= h.N_EXPOSURES - 1
    assert exposures == sorted(exposures, key=lambda e: e.midpoint_mjd)


def test_resolve_result_missing(session, definition, result_num):
    assert resolve_result(session, h.RUN_ID, 999) is None
    assert resolve_result(session, "no-such-run", 0) is None


def test_visualize_result_renders_all_four_plots(session, definition, result_num, tmp_path):
    out_dir = tmp_path / "out"
    summary = visualize_result(session, h.FakeCutoutEngine(), h.RUN_ID, result_num, out_dir)

    assert summary is not None
    assert summary.n_exposures_touched >= h.N_EXPOSURES - 1
    assert summary.n_images_found == summary.n_exposures_touched  # every touched exposure has an Image row
    assert set(summary.artifacts) == {"light_curve", "coadd", "movie", "sky"}
    for path in summary.artifacts.values():
        assert Path(path).exists()
    assert f"{h.RUN_ID} result" in summary.format()

    # cutouts are discarded -- only the four plots remain in out_dir
    assert sorted(p.name for p in out_dir.iterdir()) == ["coadd.png", "light_curve.png", "movie.gif", "sky.png"]


def test_visualize_result_missing_returns_none(session, definition, tmp_path):
    assert visualize_result(session, h.FakeCutoutEngine(), h.RUN_ID, 999, tmp_path / "out") is None


def test_cli_viz_result(session, definition, result_num, tmp_path, monkeypatch):
    monkeypatch.setattr("deep_asteroids.util.cutout.LSSTCutoutPhotometry", h.FakeCutoutEngine)
    out_dir = tmp_path / "cli_out"
    runner = CliRunner()
    result = runner.invoke(main, [
        "viz", "result", "--run-id", h.RUN_ID, "--result-num", str(result_num), "--out-dir", str(out_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "image(s) found" in result.output
    assert (out_dir / "sky.png").exists() and (out_dir / "movie.gif").exists()

    missing = runner.invoke(main, [
        "viz", "result", "--run-id", h.RUN_ID, "--result-num", "999", "--out-dir", str(out_dir),
    ])
    assert missing.exit_code != 0 and "no result 999" in missing.output
