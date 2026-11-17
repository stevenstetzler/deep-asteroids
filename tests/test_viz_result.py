"""Tests for viz/result.py and `deep-asteroids viz result`: promotes
examples/viz_result.py into a reusable CLI primitive (#27)."""
from dataclasses import dataclass
from pathlib import Path

import pytest
import result_viz_helpers as h
from click.testing import CliRunner
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.viz.result import (
    predict,
    render_sky_plot,
    resolve_result,
    touched_exposures,
    visualize_result,
)


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
    # #38: the planted mover's own gathered detections sit well within 2" of every exposure's
    # predicted position (just jitter, no deliberately-missed exposures), so every touched
    # exposure is "seen" and "coadd_seen" == "coadd" (all) -- "coadd_unseen" has nothing to
    # stack and is correctly absent; see test_visualize_result_coadd_unseen below for that case.
    assert set(summary.artifacts) == {"light_curve", "coadd", "coadd_seen", "movie", "sky"}
    assert all(m.seen for m in summary.measurements)
    for path in summary.artifacts.values():
        assert Path(path).exists()
    assert f"{h.RUN_ID} result" in summary.format()

    # cutouts are discarded -- only the plots above remain in out_dir
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "coadd.png", "coadd_seen.png", "light_curve.png", "movie.gif", "sky.png",
    ]


def test_visualize_result_coadd_seen_unseen_split(session, definition, result_num, tmp_path, monkeypatch):
    """#38: an exposure whose gathered detection is moved off the predicted track (beyond 2")
    is "unseen" -- it still gets forced-photometered (so "all" still includes it), but drops
    out of "coadd_seen" and into "coadd_unseen"."""
    from deep_asteroids.viz import result as result_mod

    real_seen = result_mod._seen
    calls = []

    def spy_seen(ra, dec, mjd_tai, gathered, **kwargs):
        seen = real_seen(ra, dec, mjd_tai, gathered, **kwargs)
        calls.append(seen)
        return seen if len(calls) % 3 else False  # force every third exposure "unseen"

    monkeypatch.setattr(result_mod, "_seen", spy_seen)

    out_dir = tmp_path / "out"
    summary = visualize_result(session, h.FakeCutoutEngine(), h.RUN_ID, result_num, out_dir)

    n_seen = sum(1 for m in summary.measurements if m.seen)
    n_unseen = sum(1 for m in summary.measurements if m.seen is False)
    assert n_seen > 0 and n_unseen > 0  # the spy above guarantees a genuine split
    assert {"coadd", "coadd_seen", "coadd_unseen"} <= set(summary.artifacts)


@dataclass
class _FakeMatched:
    """Duck-types the ``known_tracklet`` fields ``render_sky_plot()``'s ``matched`` param
    reads (#38): ``source``, and the epoch-based fit ``ingest.fit.predict()`` takes."""
    source: str
    epoch: float
    ra_epoch: float
    dec_epoch: float
    vra: float
    vdec: float


def test_render_sky_plot_draws_a_matched_trajectory(session, definition, result_num, tmp_path):
    """#38: given a ``matched`` known tracklet, the sky plot draws a second arrow for its own
    track and labels it with its source -- without touching the result's own track/legend."""
    from deep_asteroids.ingest.fit import utc_to_tai

    resolved = resolve_result(session, h.RUN_ID, result_num)
    sd, tracklet, gathered = resolved
    exposures = touched_exposures(sd, tracklet)
    exposure_times = [float(utc_to_tai(e.midpoint_mjd)) for e in exposures]  # same convention visualize_result() uses
    epoch = sum(exposure_times) / len(exposure_times)
    ra_epoch, dec_epoch = predict(tracklet, epoch)
    # a nearby object with almost the same fit, offset by ~1" in dec -- near enough that both
    # tracks share the same plotted sky region (render_sky_plot()'s own axis limits include it)
    matched = _FakeMatched(source="synthetic", epoch=epoch, ra_epoch=ra_epoch, dec_epoch=dec_epoch + 1 / 3600,
                           vra=tracklet.vra, vdec=tracklet.vdec)

    out_dir = tmp_path / "sky_matched"
    path = render_sky_plot(h.RUN_ID, result_num, tracklet, gathered, exposure_times, out_dir, matched=matched)
    assert path is not None and path.exists()

    without_match = render_sky_plot(h.RUN_ID, result_num, tracklet, gathered, exposure_times,
                                    tmp_path / "sky_unmatched")
    assert without_match is not None and without_match.exists()
    # the matched version is a strictly bigger figure (one more legend entry/arrow) --
    # a cheap, real signal that something was actually drawn differently, without parsing
    # pixels or privately inspecting the Axes after the figure is already closed
    assert path.stat().st_size != without_match.stat().st_size


def test_render_sky_plot_grid_and_arcmin_offset_axes(session, definition, result_num, tmp_path):
    """Grid on, and RA/Dec in arcmin offset from this plot's own minimum (in degrees) -- a
    plain linear offset (no cos(dec)), with the reference degree value printed in the
    label so the plot is still exactly reconstructible."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    resolved = resolve_result(session, h.RUN_ID, result_num)
    sd, tracklet, gathered = resolved
    exposure_times = [e.midpoint_mjd for e in touched_exposures(sd, tracklet)]

    fig, ax = plt.subplots()
    render_sky_plot(h.RUN_ID, result_num, tracklet, gathered, exposure_times, tmp_path, ax=ax)

    assert any(line.get_visible() for line in ax.get_xgridlines())
    assert any(line.get_visible() for line in ax.get_ygridlines())
    assert "arcmin" in ax.get_xlabel() and "arcmin" in ax.get_ylabel()
    assert ax.get_xlabel().startswith("RA - ") and " deg " in ax.get_xlabel()
    assert ax.get_ylabel().startswith("Dec - ") and " deg " in ax.get_ylabel()
    # the axis minimum is at (or very near) 0 arcmin -- it's defined as this plot's own min
    assert ax.get_xlim()[0] <= 0 <= ax.get_xlim()[1]
    assert ax.get_ylim()[0] <= 0 <= ax.get_ylim()[1]
    plt.close(fig)


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
