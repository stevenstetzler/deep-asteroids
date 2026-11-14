"""Tests for web/site.py and `deep-asteroids package result` (#27): the
static result site, one cached page per result under
web/results/<night>/<field>/ccd<detector_number>/<band>/<result_num>/index.html.
See test_package_index.py for `deep-asteroids package index`."""
from pathlib import Path

import pytest
import result_viz_helpers as h
from click.testing import CliRunner
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.web.site import band_slug, package_result, render_result_pdf, result_dir


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


@pytest.fixture
def runner():
    return CliRunner()


def test_band_slug():
    assert band_slug(None) == "all"
    assert band_slug("VR") == "VR"


def test_result_dir_layout(definition):
    path = result_dir("web", definition, 3)
    assert path == Path("web/results/20190403/T1/ccd1/all/3")


def test_package_result_renders_and_caches(session, definition, result_num, tmp_path):
    skipped, path = package_result(session, h.FakeCutoutEngine(), definition, result_num, tmp_path)
    assert not skipped and path is not None and path.name == "index.html"
    assert path.exists()
    page = path.read_text()
    assert h.RUN_ID in page and f"result {result_num}" in page
    for img in ("light_curve.png", "coadd.png", "movie.gif", "sky.png"):
        assert img in page and (path.parent / img).exists()

    pdf = path.parent / f"{h.RUN_ID}-result-{result_num}.pdf"
    assert pdf.exists() and pdf.stat().st_size > 0
    assert f'href="{pdf.name}"' in page and "Download PDF" in page

    again_skipped, again_path = package_result(session, h.FakeCutoutEngine(), definition, result_num, tmp_path)
    assert again_skipped and again_path == path

    forced_skipped, forced_path = package_result(session, h.FakeCutoutEngine(), definition, result_num, tmp_path,
                                                 force=True)
    assert not forced_skipped and forced_path == path


def test_render_result_pdf_needs_sky_coadd_light_curve(session, definition, result_num, tmp_path):
    from deep_asteroids.viz.result import visualize_result

    out_dir = tmp_path / "out"
    summary = visualize_result(session, h.FakeCutoutEngine(), h.RUN_ID, result_num, out_dir)
    pdf_path = render_result_pdf(definition, h.RUN_ID, result_num, summary, None, out_dir)
    assert pdf_path is not None and pdf_path.name == f"{h.RUN_ID}-result-{result_num}.pdf" and pdf_path.exists()

    summary.artifacts.pop("coadd")
    assert render_result_pdf(definition, h.RUN_ID, result_num, summary, None, out_dir) is None


def test_plot_functions_can_suppress_their_own_title(session, definition, result_num, tmp_path):
    """The report draws sky/coadd/light curve with title=False (the page's own title already
    names the result); the standalone png/pdf artifacts still get one (title=True, the
    default)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from deep_asteroids.viz.result import visualize_result
    from deep_asteroids.viz.tracklet import render_coadd_plot, render_light_curve_plot

    summary = visualize_result(session, h.FakeCutoutEngine(), h.RUN_ID, result_num, tmp_path / "out")

    fig, ax = plt.subplots()
    render_light_curve_plot(summary, tmp_path, ax=ax, title=False)
    assert ax.get_title() == ""
    plt.close(fig)

    fig, ax = plt.subplots()
    render_light_curve_plot(summary, tmp_path, ax=ax)
    assert ax.get_title() != ""
    plt.close(fig)

    fig, ax = plt.subplots()
    render_coadd_plot(summary, tmp_path, ax=ax, title=False)
    assert ax.get_title() == ""
    plt.close(fig)


def test_package_result_missing_result(session, definition, tmp_path):
    skipped, path = package_result(session, h.FakeCutoutEngine(), definition, 999, tmp_path)
    assert not skipped and path is None


def test_cli_package_result(session, definition, result_num, tmp_path, monkeypatch, runner):
    monkeypatch.setattr("deep_asteroids.util.cutout.LSSTCutoutPhotometry", h.FakeCutoutEngine)
    web_dir = tmp_path / "web"
    result = runner.invoke(main, ["package", "result", "--run-id", h.RUN_ID, "--web-dir", str(web_dir)])
    assert result.exit_code == 0, result.output
    assert "page(s) rendered" in result.output and "0 missing" in result.output
    n_rendered = int(result.output.split()[2])
    page = web_dir / "results" / "20190403" / "T1" / "ccd1" / "all" / str(result_num) / "index.html"
    assert page.exists()

    again = runner.invoke(main, ["package", "result", "--run-id", h.RUN_ID, "--web-dir", str(web_dir)])
    assert f"{n_rendered} already present" in again.output

    narrowed = runner.invoke(main, [
        "package", "result", "--run-id", h.RUN_ID, "--web-dir", str(web_dir), "--result-num", "999",
    ])
    assert "1 missing" in narrowed.output


def test_cli_package_result_needs_a_selection(runner, session):
    result = runner.invoke(main, ["package", "result", "--web-dir", "web"])
    assert result.exit_code != 0 and "select at least one search definition" in result.output
