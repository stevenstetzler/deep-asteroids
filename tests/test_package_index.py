"""Tests for web/index.py and `deep-asteroids package index` (#27 follow-up): the
nights -> fields -> CCDs -> bands -> results index pages above the leaf result page."""
import crossmatch_helpers as h
import pytest
from click.testing import CliRunner
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.web.index import (
    bands_rows,
    build_site_index,
    detectors_rows,
    fields_rows,
    nights_rows,
    results_rows,
)

NIGHT_1 = 20190402
NIGHT_2 = 20190403


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


@pytest.fixture
def seeded(session):
    """Three search definitions: one searched with 2 results (night 1, field A0b, ccd1,
    band VR), one defined but never searched (night 1, field A0b, ccd2, band None/"all"),
    and one searched with 1 result on a different night/field/band (night 2, field A0c,
    ccd5, band g)."""
    mover = {"ra": h.BASE[0], "dec": h.BASE[1], "vra": 0.2, "vdec": 0.05, "n": 12}
    searched = h.add_definition(session, "run-searched", night=NIGHT_1, field="A0b", detector=1, band="VR")
    h.add_run(session, "run-searched", [mover, {**mover, "ra": h.BASE[0] + 1.0}])
    unsearched = h.add_definition(session, "run-unsearched", night=NIGHT_1, field="A0b", detector=2, band=None)
    other_night = h.add_definition(session, "run-other-night", night=NIGHT_2, field="A0c", detector=5, band="g")
    h.add_run(session, "run-other-night", [mover])
    session.commit()
    return searched, unsearched, other_night


def test_nights_rows(session, seeded):
    rows = nights_rows(session)
    assert rows == [(NIGHT_1, 1, 2, 1, 2), (NIGHT_2, 1, 1, 1, 1)]


def test_fields_rows(session, seeded):
    assert fields_rows(session, NIGHT_1) == [("A0b", 2, 2, 1, 2)]
    assert fields_rows(session, NIGHT_2) == [("A0c", 1, 1, 1, 1)]


def test_detectors_rows(session, seeded):
    rows = detectors_rows(session, NIGHT_1, "A0b")
    assert rows == [(1, "D1", 1, 1, 2), (2, "D2", 1, 0, 0)]


def test_bands_rows_sorted_and_searched_flag(session, seeded):
    rows = bands_rows(session, NIGHT_1, "A0b", 1)
    # add_run()'s Search row only sets velocity_0/velocity_1 (0.1/0.5); no angle/dx, and no
    # search_run row at all (snr), so those come back None
    assert rows == [("VR", "run-searched", h.N_EXPOSURES, True, 2, 0.1, 0.5, None, None, None, None)]
    unsearched_rows = bands_rows(session, NIGHT_1, "A0b", 2)
    assert unsearched_rows == [(None, "run-unsearched", h.N_EXPOSURES, False, 0, None, None, None, None, None, None)]


def test_results_rows_ordered_by_result_num(session, seeded):
    rows = results_rows(session, "run-searched")
    assert [r[0] for r in rows] == [0, 1]
    assert all(r[1] == 12 for r in rows)  # n (Hough points)


def test_results_rows_handles_one_sided_sigma_v(session):
    """A real full-survey case (~80 of ~277k results): a degenerate single-axis fit leaves
    exactly one of sigma_vra/sigma_vdec None. math.hypot() can't take a None, so sigma_v must
    come back None too rather than crashing (this took down a real `package index` run)."""
    h.add_definition(session, "run-one-sided")
    h.add_run(session, "run-one-sided", [
        {"ra": h.BASE[0], "dec": h.BASE[1], "vra": 0.2, "vdec": 0.05, "sigma_vra": None, "sigma_vdec": 0.0},
        {"ra": h.BASE[0], "dec": h.BASE[1], "vra": 0.2, "vdec": 0.05, "sigma_vra": 0.0, "sigma_vdec": None},
    ])
    session.commit()

    rows = results_rows(session, "run-one-sided")
    assert [r[4] for r in rows] == [None, None]


def test_build_site_index_writes_every_level_and_links_resolve(session, seeded, tmp_path):
    web_dir = tmp_path / "web"
    paths = build_site_index(session, web_dir)
    assert (web_dir / "index.html").exists()
    assert len(paths) == len(set(paths))  # every page written exactly once

    root = (web_dir / "index.html").read_text()
    assert f'href="results/{NIGHT_1}/index.html"' in root and f'href="results/{NIGHT_2}/index.html"' in root
    assert "searches defined" in root and "searches executed" in root

    night_page = (web_dir / "results" / str(NIGHT_1) / "index.html").read_text()
    assert 'href="A0b/index.html"' in night_page and '<a href="../../index.html">' in night_page

    field_page = (web_dir / "results" / str(NIGHT_1) / "A0b" / "index.html").read_text()
    assert 'href="ccd1/index.html"' in field_page and 'href="ccd2/index.html"' in field_page

    ccd1_page = (web_dir / "results" / str(NIGHT_1) / "A0b" / "ccd1" / "index.html").read_text()
    assert 'href="VR/index.html"' in ccd1_page and ">yes<" in ccd1_page
    assert ">find-asteroids<" in ccd1_page and ">0.100<" in ccd1_page and ">0.500<" in ccd1_page

    ccd2_page = (web_dir / "results" / str(NIGHT_1) / "A0b" / "ccd2" / "index.html").read_text()
    assert 'href="all/index.html"' in ccd2_page and ">no<" in ccd2_page

    vr_results_page = (web_dir / "results" / str(NIGHT_1) / "A0b" / "ccd1" / "VR" / "index.html").read_text()
    assert 'href="0/index.html"' in vr_results_page and 'href="1/index.html"' in vr_results_page

    # a search_definition with no results yet still gets a page -- empty, not missing
    all_results_page = (web_dir / "results" / str(NIGHT_1) / "A0b" / "ccd2" / "all" / "index.html").read_text()
    assert "No results ingested yet" in all_results_page


def test_build_site_index_corrects_stale_content(session, seeded, tmp_path):
    web_dir = tmp_path / "web"
    build_site_index(session, web_dir)
    page = web_dir / "results" / str(NIGHT_1) / "A0b" / "ccd1" / "VR" / "index.html"
    page.write_text("stale")
    build_site_index(session, web_dir)
    assert page.read_text() != "stale"


def test_build_site_index_skips_the_write_when_content_is_unchanged(session, seeded, tmp_path):
    """A re-run with nothing changed in the database shouldn't touch pages on disk at all --
    at full-survey scale the write, not the query, is what's slow (see web/index.py's own
    module docstring), and this is also what makes a re-run after an interruption (e.g. a
    preempted Slurm job) cheap instead of starting over from a cold file-create for
    everything."""
    web_dir = tmp_path / "web"
    build_site_index(session, web_dir)
    page = web_dir / "results" / str(NIGHT_1) / "A0b" / "ccd1" / "VR" / "index.html"
    mtime_before = page.stat().st_mtime_ns

    build_site_index(session, web_dir)
    assert page.stat().st_mtime_ns == mtime_before


def test_cli_package_index(session, seeded, tmp_path):
    web_dir = tmp_path / "web"
    result = CliRunner().invoke(main, ["package", "index", "--web-dir", str(web_dir)])
    assert result.exit_code == 0, result.output
    assert "page(s) written" in result.output
    assert (web_dir / "index.html").exists()


def test_build_site_index_can_be_restricted_to_nights(session, seeded, tmp_path):
    everything = tmp_path / "all"
    build_site_index(session, everything)
    assert (everything / "results" / str(NIGHT_1)).exists() and (everything / "results" / str(NIGHT_2)).exists()

    one = tmp_path / "one"
    paths = build_site_index(session, one, nights={NIGHT_1})
    assert (one / "results" / str(NIGHT_1)).exists() and not (one / "results" / str(NIGHT_2)).exists()
    # the top page still lists every night, and the night's own pages are the same as in the full build
    assert (one / "index.html").read_text() == (everything / "index.html").read_text()
    assert sorted(p.relative_to(one) for p in paths) == sorted(
        p.relative_to(everything) for p in everything.rglob("index.html") if f"results/{NIGHT_2}" not in str(p))
    for path in paths:
        assert path.read_text() == (everything / path.relative_to(one)).read_text()

    none = tmp_path / "none"
    build_site_index(session, none, nights={1999_01_01})  # a night with nothing: only the top page
    assert (none / "index.html").exists() and not (none / "results").exists()


def test_cli_package_index_night(session, seeded, tmp_path):
    out = CliRunner().invoke(main, ["package", "index", "--web-dir", str(tmp_path), "--night", str(NIGHT_1)])
    assert out.exit_code == 0, out.output
    assert (tmp_path / "results" / str(NIGHT_1)).exists() and not (tmp_path / "results" / str(NIGHT_2)).exists()
