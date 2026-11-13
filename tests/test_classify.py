import crossmatch_helpers as h
import pytest
from click.testing import CliRunner
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.crossmatch.classify import (
    KNOWN,
    SUBMITTED,
    SYNTHETIC,
    UNMATCHED_IN,
    UNMATCHED_OUT,
    MissingStatisticsError,
    ResultRow,
    Thresholds,
    classify,
    funnel,
    load_results,
    retained,
    why_rejected,
)
from deep_asteroids.crossmatch.dedup import dedup_results
from deep_asteroids.crossmatch.match import crossmatch_results
from deep_asteroids.crossmatch.statistics import compute_result_statistics
from deep_asteroids.database import create_db

N = h.N_EXPOSURES


def row(**kw):
    base = dict(result_id=1, run_id="r", result_num=0, night=h.NIGHT, authoritative=True, n_gathered=50,
                median_residual_arcsec=0.2, speed=0.2, sigma_v=1e-5, median_mag=22.0, velocity_0=0.1, velocity_1=0.5,
                best={})
    base.update(kw)
    return ResultRow(**base)


def test_groups_follow_the_closest_match_within_each_radius():
    rows = classify([
        row(best={"mpc_linked": 1.0}),                                  # submitted
        row(best={"synthetic": 1.5}),                                   # synthetic
        row(best={"mpc_linked": 1.9, "synthetic": 0.5}),                # both within 2": the closer
        row(best={"mpc_linked": 0.5, "synthetic": 1.0}),
        row(best={"mpc_linked": 2.5, "mpchecker": 9.0}),                # submitted too far; known within 10"
        row(best={"mpchecker": 1.0, "mpc_linked": 0.9}),                # known is closer, but submitted outranks it
        row(best={"mpc_linked": 3.0, "mpchecker": 11.0}),               # nothing within radius
        row(best={}, speed=0.7),                                        # unmatched, outside the velocity range
    ])
    assert [r.group for r in rows] == [SUBMITTED, SYNTHETIC, SYNTHETIC, SUBMITTED, KNOWN, SUBMITTED, UNMATCHED_IN,
                                       UNMATCHED_OUT]


def test_radii_are_thresholds():
    r = classify([row(best={"mpc_linked": 1.0})], Thresholds(submitted_radius_arcsec=0.5))[0]
    assert r.group == UNMATCHED_IN
    assert classify([row(best={"mpchecker": 15.0})], Thresholds(known_radius_arcsec=20.0))[0].group == KNOWN


def test_putative_real_is_a_cut_on_residual_gathered_points_and_speed():
    thr = Thresholds()  # residual < 10**-0.2 = 0.631", 10 < n_gathered < 150, speed strictly inside (0.1, 0.5)
    cases = {
        "good": (row(), True, ()),
        "just under the residual cut": (row(median_residual_arcsec=0.63), True, ()),
        "residual too high": (row(median_residual_arcsec=0.64), False, ("residual",)),
        "no residual at all": (row(median_residual_arcsec=None), False, ("residual",)),
        "10 points is not more than 10": (row(n_gathered=10), False, ("n_gathered",)),
        "11 points": (row(n_gathered=11), True, ()),
        "149 points": (row(n_gathered=149), True, ()),
        "150 points is not fewer than 150": (row(n_gathered=150), False, ("n_gathered",)),
        "speed on the lower edge": (row(speed=0.1), False, ("velocity",)),
        "speed on the upper edge": (row(speed=0.5), False, ("velocity",)),
        "everything wrong": (row(median_residual_arcsec=5.0, n_gathered=3, speed=0.9), False,
                             ("residual", "n_gathered", "velocity")),
    }
    for name, (r, real, failed) in cases.items():
        classify([r], thr)
        assert (r.putative_real, r.failed) == (real, failed), name


def test_putative_new_is_real_and_unmatched():
    rows = classify([row(), row(best={"mpc_linked": 0.1}), row(median_residual_arcsec=9.0)])
    assert [r.putative_new for r in rows] == [True, False, False]


def test_funnel_counts_distinct_objects_and_what_the_cut_keeps():
    rows = classify([
        row(result_id=1, best={"mpc_linked": 0.1}),
        row(result_id=2, best={"mpc_linked": 0.1}, median_residual_arcsec=3.0),  # a known object the cut loses
        row(result_id=3, best={"synthetic": 0.1}),
        row(result_id=4),                                                       # putative new
        row(result_id=5, median_residual_arcsec=4.0),                           # noise, in range
        row(result_id=6, speed=0.9, median_residual_arcsec=4.0),                # noise, out of range
        row(result_id=7, authoritative=False),                                  # a redetection of another result
    ])
    f = funnel(rows)
    assert (f.n_results, f.n_objects, f.n_putative_real, f.n_putative_new) == (7, 6, 3, 1)
    assert f.by_group[SUBMITTED] == (2, 1) and f.by_group[SYNTHETIC] == (1, 1) and f.by_group[KNOWN] == (0, 0)
    assert f.by_group[UNMATCHED_IN] == (2, 1) and f.by_group[UNMATCHED_OUT] == (1, 0)
    assert retained(rows)[SUBMITTED] == (1, 2) and retained(rows)[KNOWN] == (0, 0)
    assert why_rejected(rows) == {"residual": 3, "velocity": 1}  # result 7 is not an object, so is not counted
    text = f.format()
    assert "putative new (putative real, matching nothing): 1" in text and "submitted" in text


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


def _night(session):
    """One search with a result on a submitted tracklet, one that is noise, one a redetection of the first."""
    h.add_definition(session, "run")
    a, b, c = h.add_run(session, "run", [
        {"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05, "n": 50},
        {"ra": 215.05, "dec": -13.0, "vra": 0.25, "vdec": 0.0, "n": 20},
        {"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05, "n": 10},
    ])
    for _ in range(2):  # two gathered points per exposure: the cut wants more than 10 points
        h.add_gathered(session, a, [0.2] * N, mags=[21.0] * N)
        h.add_gathered(session, b, [4.0 if i % 2 else -4.0 for i in range(N)], vra=0.25, vdec=0.0, ra=215.05)
        h.add_gathered(session, c, [0.3] * N)
    h.add_known(session, "mpc_linked", 215.0, -13.0, 0.2, 0.05, n_members=1)
    session.commit()
    return a, b, c


def test_load_results_joins_statistics_matches_and_dedup(session):
    _night(session)
    with pytest.raises(MissingStatisticsError, match="run `deep-asteroids statistics`"):
        load_results(session)
    compute_result_statistics(session)
    crossmatch_results(session)
    dedup_results(session)
    rows = {r.result_num: r for r in classify(load_results(session, nights=[h.NIGHT]))}
    assert rows[0].authoritative and not rows[2].authoritative and rows[1].authoritative  # 0 and 2 are one object
    assert rows[0].group == SUBMITTED and rows[0].putative_real and rows[0].median_mag == 21.0
    assert rows[1].group == UNMATCHED_IN and not rows[1].putative_real and rows[1].failed == ("residual",)
    assert rows[0].best["mpc_linked"] < 0.1 and rows[1].best == {}
    assert rows[0].velocity_0 == 0.1 and rows[0].night == h.NIGHT
    assert load_results(session, run_ids=["nope"]) == [] and load_results(session, nights=[20000101]) == []


def test_results_never_deduplicated_count_as_their_own_object(session):
    _night(session)
    compute_result_statistics(session)
    assert all(r.authoritative for r in load_results(session))


def test_cli_summary_and_query(db_url):
    runner = CliRunner()
    with Session(create_db()) as session:
        _night(session)
    assert "run `deep-asteroids statistics` first" in runner.invoke(main, ["summary"]).output
    assert runner.invoke(main, ["statistics"]).exit_code == 0
    assert runner.invoke(main, ["crossmatch"]).exit_code == 0
    assert runner.invoke(main, ["dedup"]).exit_code == 0

    out = runner.invoke(main, ["summary", "--night", str(h.NIGHT)])
    assert out.exit_code == 0, out.output
    assert "3 / 2" in out.output  # 3 results, 2 distinct objects
    assert "submitted" in out.output and "the cut keeps: submitted 1/1 (100%)" in out.output
    assert "results failing each cut: residual 1" in out.output
    assert "no results selected" in runner.invoke(main, ["summary", "--night", "19000101"]).output

    out = runner.invoke(main, ["query", "results", "--header", "--authoritative-only"])
    lines = [line.split("|") for line in out.output.strip().splitlines()]
    assert lines[0][:6] == ["run_id", "result_num", "authoritative", "group", "putative_real", "failed_cuts"]
    by_num = {line[1]: line for line in lines[1:]}
    assert set(by_num) == {"0", "1"}  # the redetection (result 2) is not authoritative
    assert by_num["0"][3:6] == ["submitted", "1", ""] and by_num["1"][3:6] == ["unmatched (in velocity range)", "0", "residual"]
    assert by_num["0"][11] != "" and by_num["1"][11] == ""  # sep_submitted

    new = runner.invoke(main, ["query", "results", "--putative-new"])
    assert new.output.strip() == ""
    rejected = runner.invoke(main, ["query", "results", "--not-putative-real", "--group", "unmatched (in velocity range)"])
    assert [line.split("|")[1] for line in rejected.output.strip().splitlines()] == ["1"]
    limited = runner.invoke(main, ["query", "results", "--limit", "1"])
    assert len(limited.output.strip().splitlines()) == 1
    loose = runner.invoke(main, ["query", "results", "--putative-new", "--log10-residual", "2"])
    assert [line.split("|")[1] for line in loose.output.strip().splitlines()] == ["1"]  # nothing else is unmatched
