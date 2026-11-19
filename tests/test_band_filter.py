"""``--band``: every stage can be run on one band's searches only (the undefined-band legacy searches,
``band IS NULL``, or e.g. ``VR``), so a band's results are never mixed with another's."""
import crossmatch_helpers as h
import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.crossmatch.classify import load_results
from deep_asteroids.crossmatch.completeness import completeness_rows, missed_known_objects
from deep_asteroids.crossmatch.dedup import dedup_results
from deep_asteroids.crossmatch.match import crossmatch_results
from deep_asteroids.crossmatch.statistics import compute_result_statistics
from deep_asteroids.database import create_db
from deep_asteroids.database.crossmatch import CrossmatchRun, ResultDedup
from deep_asteroids.database.search import parse_band

CHIP = (h.BASE[0], h.BASE[1], 0.1)
V = {"vra": 0.2, "vdec": 0.0}
LEGACY, VR = "run", "run-VR"
NORTH = 0.01  # deg: 36", far enough that a result matches one object only


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


def _scene(session):
    """One chip searched twice: ``run`` (band NULL) has a result on object A, ``run-VR`` (band VR) a result on
    object B 36" north of it. Every result's own object is matched at 0", the other's is too far to be a candidate."""
    h.add_definition(session, LEGACY, footprint=CHIP)
    h.add_definition(session, VR, band="VR", footprint=CHIP)
    legacy = h.add_run(session, LEGACY, [{"ra": h.BASE[0], "dec": h.BASE[1], **V}])[0]
    vr = h.add_run(session, VR, [{"ra": h.BASE[0], "dec": h.BASE[1] + NORTH, **V}])[0]
    a = h.add_known(session, "synthetic", h.BASE[0], h.BASE[1], 0.2, 0.0, orbit_id=1, component="primary", mag=21.0)
    b = h.add_known(session, "synthetic", h.BASE[0], h.BASE[1] + NORTH, 0.2, 0.0, orbit_id=2, component="primary",
                    mag=22.0)
    session.commit()
    return legacy, vr, a, b


def test_parse_band():
    assert parse_band(None) is Ellipsis  # not given: any band
    assert parse_band("") is None and parse_band("none") is None and parse_band("None") is None
    assert parse_band("VR") == "VR"


def test_crossmatch_only_the_selected_band(session):
    _scene(session)
    summary = crossmatch_results(session, band="VR")
    assert summary.runs == [VR]
    assert [r.run_id for r in session.scalars(select(CrossmatchRun))] == [VR]
    assert crossmatch_results(session, band=None).runs == [LEGACY]  # the undefined-band search
    rest = crossmatch_results(session)  # both are done now
    assert rest.runs == [] and set(rest.skipped_runs) == {LEGACY, VR}


def test_statistics_only_the_selected_band(session):
    _scene(session)
    assert compute_result_statistics(session, band="VR").runs == [VR]
    assert compute_result_statistics(session, band=None).runs == [LEGACY]


def test_dedup_clusters_within_a_band_only(session):
    h.add_definition(session, LEGACY)
    h.add_definition(session, VR, band="VR")
    # the same object found by both searches, 0.5" apart; the VR one has more Hough points
    legacy = h.add_run(session, LEGACY, [{"ra": h.BASE[0], "dec": h.BASE[1], "n": 8, **V}])[0]
    vr = h.add_run(session, VR, [{"ra": h.BASE[0], "dec": h.BASE[1] + 0.5 / 3600, "n": 12, **V}])[0]
    session.commit()

    both = dedup_results(session)  # across bands, they are one object
    assert both.n_clusters == 1
    only_vr = dedup_results(session, band="VR", force=True)
    assert (only_vr.n_results, only_vr.n_clusters) == (1, 1)
    rows = {r.result_id: r for r in session.scalars(select(ResultDedup))}
    assert rows[vr.id].n_members == 1 and rows[vr.id].is_authoritative  # clustered alone now
    only_legacy = dedup_results(session, band=None, force=True)
    assert (only_legacy.n_results, only_legacy.n_clusters) == (1, 1)
    rows = {r.result_id: r for r in session.scalars(select(ResultDedup))}
    assert rows[legacy.id].n_members == 1 and rows[legacy.id].is_authoritative and rows[vr.id].n_members == 1


def test_load_results_by_band(session):
    _scene(session)
    compute_result_statistics(session)
    assert {r.run_id for r in load_results(session)} == {LEGACY, VR}
    assert {r.run_id for r in load_results(session, band="VR")} == {VR}
    assert {r.run_id for r in load_results(session, band=None)} == {LEGACY}
    assert load_results(session, band="r") == []


def test_a_band_is_recovered_only_by_its_own_results(session):
    _, _, a, b = _scene(session)
    crossmatch_results(session)

    def recovered(**kw):
        return {r.mag: r.recovered for r in completeness_rows(session, **kw)}

    assert recovered() == {21.0: True, 22.0: True}  # any search
    assert recovered(band="VR") == {21.0: False, 22.0: True}  # A was only found by the legacy search
    assert recovered(band=None) == {21.0: True, 22.0: False}
    assert {r.run_id for r in completeness_rows(session, band="VR")} == {VR}
    missed_vr = missed_known_objects(session, band="VR")  # object A: only the legacy search found it
    assert [m.label for m in missed_vr] == ["orbit 1 (primary)"]
    assert missed_known_objects(session) == []


@pytest.mark.parametrize("command", [["summary"], ["plot", "completeness"], ["plot", "magnitudes"],
                                     ["plot", "sigma-v"], ["plot", "residuals"], ["missed-objects"],
                                     ["query", "results"], ["dedup"], ["crossmatch"], ["statistics"]])
def test_every_stage_takes_a_band(command):
    out = CliRunner().invoke(main, [*command, "--help"]).output
    assert "--band" in out


def test_cli_summary_selects_a_band(session, db_url):
    _scene(session)
    crossmatch_results(session)
    compute_result_statistics(session)
    runner = CliRunner()
    both = runner.invoke(main, ["summary", "--all-results"])
    vr = runner.invoke(main, ["summary", "--all-results", "--band", "VR"])
    legacy = runner.invoke(main, ["summary", "--all-results", "--band", "none"])
    assert both.exit_code == vr.exit_code == legacy.exit_code == 0, (both.output, vr.output, legacy.output)
    assert "2 / 2" in both.output and "1 / 1" in vr.output and "1 / 1" in legacy.output
    assert runner.invoke(main, ["summary", "--band", "no_such_band"]).exit_code != 0
