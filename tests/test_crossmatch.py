import crossmatch_helpers as h
import numpy as np
import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.crossmatch.dedup import dedup_results
from deep_asteroids.crossmatch.geometry import pair_separations, pairs_within, positions
from deep_asteroids.crossmatch.match import (
    crossmatch_results,
    match_counts,
    match_table,
)
from deep_asteroids.database import create_db
from deep_asteroids.database.crossmatch import CrossmatchCandidate, CrossmatchRun
from deep_asteroids.ingest.fit import sep_arcsec

V = {"vra": 0.2, "vdec": 0.05}


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


def _result(east=0.0, north=0.0, **kw):
    ra, dec = h.offset(*h.BASE, east, north)
    return {"ra": ra, "dec": dec, **V, **kw}


def _known(session, source="mpc_linked", east=0.0, north=0.0, night=h.NIGHT, **kw):
    ra, dec = h.offset(*h.BASE, east, north)
    return h.add_known(session, source, ra, dec, V["vra"], V["vdec"], night=night, **kw)


def _candidates(session):
    return session.scalars(select(CrossmatchCandidate).order_by(CrossmatchCandidate.id)).all()


def test_matches_by_mean_separation_over_the_exposures(session):
    h.add_definition(session, "run")
    r = h.add_run(session, "run", [_result()])
    exact = _known(session, trkid="exact")
    offset = _known(session, source="mpchecker", east=1.0, provid="one-arcsec")
    _known(session, source="synthetic", north=60.0, orbit_id=1)  # beyond 30"
    summary = crossmatch_results(session)
    assert summary.runs == ["run"] and summary.n_results == 1 and summary.n_candidates == 2

    by_known = {c.known_tracklet_id: c for c in _candidates(session)}
    assert set(by_known) == {exact.id, offset.id}
    c = by_known[exact.id]
    assert c.result_id == r[0].id and c.source == "mpc_linked" and c.night == h.NIGHT
    assert c.n_times == h.N_EXPOSURES and c.mean_sep_arcsec < 1e-3 and c.max_sep_arcsec < 1e-3
    assert c.total_sep_arcsec == pytest.approx(c.mean_sep_arcsec * c.n_times) and c.velocity_diff < 1e-9
    assert by_known[offset.id].mean_sep_arcsec == pytest.approx(1.0, abs=0.01)
    assert by_known[offset.id].source == "mpchecker"


def test_a_velocity_difference_is_measured_and_raises_the_mean_separation(session):
    h.add_definition(session, "run")
    h.add_run(session, "run", [_result()])
    ra, dec = h.BASE
    k = h.add_known(session, "synthetic", ra, dec, V["vra"] + 0.1, V["vdec"], orbit_id=2, component="primary")
    crossmatch_results(session)
    (c,) = _candidates(session)
    assert c.known_tracklet_id == k.id
    assert c.velocity_diff == pytest.approx(0.1 * np.cos(np.radians(dec)), rel=1e-3)
    # 0.1 deg/day over +-0.047 day: ~17" at the ends, 0 at T_REF, ~9" on average
    assert c.min_sep_arcsec < 0.5 and 10 < c.max_sep_arcsec < 25 and 5 < c.mean_sep_arcsec < 12


def test_only_the_same_night_and_only_fitted_tracklets_count(session):
    h.add_definition(session, "run")
    h.add_run(session, "run", [_result()])
    _known(session, night=h.NIGHT + 1, trkid="other night")
    _known(session, flag="too_few_points", trkid="no fit")
    ok = _known(session, flag="large_residual", trkid="suspect fit is kept")
    crossmatch_results(session)
    assert [c.known_tracklet_id for c in _candidates(session)] == [ok.id]


def test_source_selection_and_max_separation(session):
    h.add_definition(session, "run")
    h.add_run(session, "run", [_result()])
    _known(session, source="mpc_linked", east=1.0)
    _known(session, source="mpchecker", east=5.0)
    crossmatch_results(session, sources=("mpchecker",), max_sep_arcsec=10.0)
    assert [c.source for c in _candidates(session)] == ["mpchecker"]
    row = session.scalars(select(CrossmatchRun)).one()
    assert (row.sources, row.max_sep_arcsec, row.n_known_tracklets, row.n_candidates) == ("mpchecker", 10.0, 1, 1)
    assert crossmatch_results(session, max_sep_arcsec=3.0, force=True).n_candidates == 1  # only the 1" one
    assert [c.source for c in _candidates(session)] == ["mpc_linked"]


def test_a_result_labelled_half_a_second_late_still_matches(session):
    """A search whose exposure labels are 0.5 s off still matches at ~0.005": no correction is needed."""
    h.add_definition(session, "run")
    late = _result()
    late["ra"], late["dec"] = late["ra"] + 0.5 * V["vra"] / 86400, late["dec"] + 0.5 * V["vdec"] / 86400
    h.add_run(session, "run", [late])
    _known(session)
    crossmatch_results(session)
    (c,) = _candidates(session)
    assert 0.001 < c.mean_sep_arcsec < 0.02


def test_idempotent_per_run_and_force_replaces(session):
    h.add_definition(session, "run")
    h.add_definition(session, "run2", detector=2)
    h.add_run(session, "run", [_result()])
    h.add_run(session, "run2", [_result(east=0.5)])
    _known(session)
    first = crossmatch_results(session, run_ids=["run"])
    assert first.runs == ["run"] and len(_candidates(session)) == 1
    again = crossmatch_results(session)
    assert again.skipped_runs == ["run"] and again.runs == ["run2"] and len(_candidates(session)) == 2
    forced = crossmatch_results(session, force=True)
    assert sorted(forced.runs) == ["run", "run2"] and len(_candidates(session)) == 2
    assert len(session.scalars(select(CrossmatchRun)).all()) == 2


def test_a_run_with_no_candidates_is_still_recorded(session):
    h.add_definition(session, "run")
    h.add_run(session, "run", [_result()])
    crossmatch_results(session)  # no known tracklets at all
    assert _candidates(session) == [] and session.scalars(select(CrossmatchRun)).one().n_candidates == 0
    assert crossmatch_results(session).skipped_runs == ["run"]


def test_runs_without_a_search_definition_or_results_are_not_matched(session):
    h.add_run(session, "orphan", [_result()])
    h.add_definition(session, "empty")
    assert crossmatch_results(session).runs == []


def test_match_counts_objects_and_table(session):
    h.add_definition(session, "run")
    h.add_definition(session, "run_b", detector=2)
    h.add_run(session, "run", [_result(), _result(east=0.2, n=99), _result(north=6.0)])
    h.add_run(session, "run_b", [])
    _known(session, source="mpc_linked", trkid="t")  # matches results 0 and 1
    _known(session, source="mpchecker", north=6.0)  # 0" from result 2, and 6" (< 10") from results 0 and 1
    _known(session, source="synthetic", east=20.0, orbit_id=3)  # 20": a candidate, but not a match
    crossmatch_results(session)
    dedup_results(session)  # results 0 and 1 are redetections; 1 has the most points
    counts = match_counts(session, ["run", "run_b"])
    assert counts["n_results"] == 3 and counts["n_objects"] == 2
    assert counts["mpc_linked"] == (2, 1) and counts["mpchecker"] == (3, 2) and counts["synthetic"] == (0, 0)
    table = match_table(counts)
    assert 'submitted < 2"' in table and 'known < 10"' in table and 'synthetic < 2"' in table
    # without de-duplication, every result is its own object
    from sqlalchemy import delete

    from deep_asteroids.database.crossmatch import ResultDedup
    session.execute(delete(ResultDedup))
    assert match_counts(session, ["run"])["n_objects"] == 3


def test_pairs_within_loses_nothing_a_brute_force_mean_test_would_keep():
    rng = np.random.default_rng(1)
    n_r, n_k = 40, 80
    r_v = rng.uniform(-0.3, 0.3, (n_r, 2))
    k_v = rng.uniform(-0.3, 0.3, (n_k, 2))
    r_ra, r_dec = positions(h.BASE[0] - r_v[:, 0] * h.T_REF + rng.uniform(-40, 40, n_r) * h.ARCSEC,
                            h.BASE[1] - r_v[:, 1] * h.T_REF + rng.uniform(-40, 40, n_r) * h.ARCSEC,
                            r_v[:, 0], r_v[:, 1], h.EXPOSURES_TAI)
    k_ra, k_dec = positions(h.BASE[0] - k_v[:, 0] * h.T_REF + rng.uniform(-40, 40, n_k) * h.ARCSEC,
                            h.BASE[1] - k_v[:, 1] * h.T_REF + rng.uniform(-40, 40, n_k) * h.ARCSEC,
                            k_v[:, 0], k_v[:, 1], h.EXPOSURES_TAI)
    radius = 30.0
    pairs = pairs_within(r_ra, r_dec, radius, k_ra, k_dec)
    fast = {(int(i), int(j)) for (i, j), m in zip(pairs, pair_separations(r_ra, r_dec, k_ra, k_dec, pairs).mean(axis=1))
            if m < radius}
    brute = {(i, j) for i in range(n_r) for j in range(n_k)
             if sep_arcsec(r_ra[i], r_dec[i], k_ra[j], k_dec[j]).mean() < radius}
    assert fast == brute and 0 < len(brute) < n_r * n_k
    assert pairs_within(r_ra[:0], r_dec[:0], radius).shape == (0, 2)


def test_cli(db_url):
    runner = CliRunner()
    with Session(create_db()) as session:
        h.add_definition(session, "run")
        h.add_definition(session, "run_b", detector=2)
        h.add_run(session, "run", [_result(), _result(east=0.3, n=50)])
        h.add_run(session, "run_b", [_result(north=90.0)])
        _known(session, trkid="TRK1", source="mpc_linked")
        _known(session, source="mpchecker", provid="2000 AA", east=4.0)
        session.commit()

    out = runner.invoke(main, ["dedup"])
    assert out.exit_code == 0, out.output
    assert "3 result(s) -> 2 distinct object(s); 1 redetection(s) merged away" in out.output
    assert "0 pointing group(s) processed, 1 skipped" in runner.invoke(main, ["dedup"]).output
    assert "1 pointing group(s) processed, 0 skipped" in runner.invoke(main, ["dedup", "--force"]).output

    out = runner.invoke(main, ["crossmatch", "mpc-linked", "mpc-ephemeris", "--night", str(h.NIGHT)])
    assert out.exit_code == 0, out.output
    assert "2 run(s) matched" in out.output and "4 candidate pair(s) stored" in out.output
    assert 'submitted < 2"' in out.output
    assert "0 run(s) matched, 2 skipped" in runner.invoke(main, ["crossmatch"]).output

    out = runner.invoke(main, ["query", "crossmatches", "--header", "--run-id", "run"])
    assert out.exit_code == 0, out.output
    lines = [line.split("|") for line in out.output.strip().splitlines()]
    assert lines[0][:4] == ["run_id", "result_num", "authoritative", "source"]
    rows = {(r[1], r[3]): r for r in lines[1:]}
    assert set(rows) == {("0", "mpc_linked"), ("0", "mpchecker"), ("1", "mpc_linked"), ("1", "mpchecker")}
    assert rows[("1", "mpc_linked")][2] == "1" and rows[("0", "mpc_linked")][2] == "0"  # authoritative
    assert rows[("0", "mpc_linked")][9] == "TRK1" and rows[("0", "mpc_linked")][10] == ""
    assert rows[("0", "mpchecker")][10] == "2000 AA" and rows[("0", "mpchecker")][9] == ""

    only_auth = runner.invoke(main, ["query", "crossmatches", "--authoritative-only", "--source", "mpchecker"])
    assert [l.split("|")[1] for l in only_auth.output.strip().splitlines()] == ["1"]
    far = runner.invoke(main, ["query", "crossmatches", "--max-mean-sep-arcsec", "1", "--all"])
    assert all(l.split("|")[3] == "mpc_linked" for l in far.output.strip().splitlines())


def test_unlinked_mpc_tracklets_are_matched_only_when_asked_for(session):
    h.add_definition(session, "run")
    h.add_run(session, "run", [_result()])
    _known(session, source="mpc_observation", trkid="fragment")
    linked = _known(session, source="mpc_linked", n_members=2)
    crossmatch_results(session)
    assert [c.known_tracklet_id for c in _candidates(session)] == [linked.id]  # the default: linked only
    crossmatch_results(session, sources=("mpc_observation",), force=True)
    assert [c.source for c in _candidates(session)] == ["mpc_observation"]
    counts = match_counts(session, ["run"])
    assert "mpc_observation" in counts and "mpc_linked" not in counts  # only what the run used is reported
    assert 'submitted (unlinked) < 2"' in match_table(counts)
