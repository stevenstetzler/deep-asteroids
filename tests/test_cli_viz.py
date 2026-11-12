from datetime import datetime, timezone

import pytest
from click.testing import CliRunner
from sqlalchemy import text
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database.images import Image

NIGHT = 20190402


@pytest.fixture
def runner(db_url, deep_db_url, monkeypatch):
    from tests.test_viz_tracklet import FakeCutoutEngine

    monkeypatch.setattr("deep_asteroids.util.cutout.LSSTCutoutPhotometry", lambda: FakeCutoutEngine())
    return CliRunner()


def _seed(runner_fixture):
    from deep_asteroids.database import create_db, get_deep_db

    with Session(create_db()) as session:
        for source in ("synthetic", "mpc_observation", "mpchecker"):
            from deep_asteroids.ingest.tracklets import ingest_source

            ingest_source(session, get_deep_db(), source)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = []
        with get_deep_db().connect() as conn:
            for detector_number in (4, 5):
                ids = conn.execute(text(
                    "SELECT de.id FROM detector_exposure de JOIN detector d ON d.id = de.detector_id "
                    "WHERE d.number = :n ORDER BY de.id"
                ), {"n": detector_number}).scalars().all()
                for i, detector_exposure_id in enumerate(ids):
                    rows.append(Image(
                        night=NIGHT, field="A0b", detector_number=detector_number, expnum=845580 + i,
                        deep_db_exposure_id=i + 1, deep_db_detector_exposure_id=detector_exposure_id,
                        product_type="differenceExp", uri=f"file:///fake/{detector_number}_{i}.fits",
                        created_at=now,
                    ))
        session.add_all(rows)
        session.commit()


def test_viz_tracklet_single_object(runner, tmp_path):
    _seed(runner)
    out_dir = tmp_path / "out"
    result = runner.invoke(main, [
        "viz", "tracklet", "--source", "mpchecker", "--night", str(NIGHT), "--provid", "2019 GA179",
        "--out-dir", str(out_dir),
    ])
    assert result.exit_code == 0, result.output
    assert "2 image(s) found" in result.output
    assert (out_dir / "light_curve.png").exists()
    assert (out_dir / "coadd.png").exists()
    assert (out_dir / "movie.gif").exists()


def test_viz_tracklet_binary_renders_all_components(runner, tmp_path, monkeypatch):
    """No --out-dir given -- defaults to ./viz/<source>/<night>/<identity>/,
    so this chdir's into tmp_path first rather than writing into the
    repo's real working directory."""
    monkeypatch.chdir(tmp_path)
    _seed(runner)
    result = runner.invoke(main, [
        "viz", "tracklet", "--source", "synthetic", "--night", str(NIGHT), "--orbit-id", "1002",
    ])
    assert result.exit_code == 0, result.output
    for component in ("primary", "companion", "barycenter", "photocenter"):
        assert component in result.output
        assert (tmp_path / "viz" / "synthetic" / str(NIGHT) / f"orbit_1002_{component}" / "light_curve.png").exists()


def test_viz_tracklet_binary_with_explicit_out_dir_does_not_clobber(runner, tmp_path):
    """Regression test: all four components used to write into the same
    --out-dir, silently overwriting each other's plots (and even sharing
    cutout filenames, since a binary's components often touch the exact
    same exposures)."""
    _seed(runner)
    out_dir = tmp_path / "shared"
    result = runner.invoke(main, [
        "viz", "tracklet", "--source", "synthetic", "--night", str(NIGHT), "--orbit-id", "1002",
        "--out-dir", str(out_dir),
    ])
    assert result.exit_code == 0, result.output
    for component in ("primary", "companion", "barycenter", "photocenter"):
        assert (out_dir / component / "light_curve.png").exists()
        assert (out_dir / component / "coadd.png").exists()


def test_viz_tracklet_requires_an_identity(runner):
    result = runner.invoke(main, ["viz", "tracklet", "--source", "mpchecker", "--night", str(NIGHT)])
    assert result.exit_code != 0
    assert "give at least one of" in result.output


def test_viz_tracklet_unknown_identity(runner):
    _seed(runner)
    result = runner.invoke(main, [
        "viz", "tracklet", "--source", "mpchecker", "--night", str(NIGHT), "--provid", "nope",
    ])
    assert result.exit_code != 0
    assert "no known_tracklet row matches" in result.output
