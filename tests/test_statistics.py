import crossmatch_helpers as h
import numpy as np
import pytest
from click.testing import CliRunner
from find_asteroids.models import Tracklet
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.crossmatch.statistics import MAX_RESIDUAL_ARCSEC, compute_result_statistics
from deep_asteroids.database import create_db
from deep_asteroids.database.statistics import ResultStatistics

N = h.N_EXPOSURES


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


def _stats(session):
    return {s.result_id: s for s in session.scalars(select(ResultStatistics))}


def test_statistics_match_hand_computed_values(session):
    h.add_definition(session, "run")
    on, off, empty = h.add_run(session, "run", [
        {"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05},
        {"ra": 215.0, "dec": -13.0, "vra": 0.3, "vdec": -0.1, "sigma_vra": None, "sigma_vdec": None},
        {"ra": 216.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05},
    ])
    # on its tracklet: a constant 0.3" offset north in every exposure (so the median residual is 0.3")
    h.add_gathered(session, on, [0.3] * N, mags=[22.0 + 0.1 * i for i in range(N)])
    # 4 of 10 points 5" off (outliers), the rest exact: median residual 0, 4 outliers over 1"
    h.add_gathered(session, off, [5.0 if i < 4 else 0.0 for i in range(N)], vra=0.3, vdec=-0.1)
    tracklet = session.scalars(select(Tracklet).where(Tracklet.result_id == on.id)).one()
    tracklet.sigma_vra, tracklet.sigma_vdec = 3e-5, 4e-5
    session.commit()

    summary = compute_result_statistics(session)
    assert summary.runs == ["run"] and summary.n_results == 3 and summary.n_empty == 1
    s = _stats(session)
    assert (s[on.id].n_gathered, s[on.id].n_times, s[on.id].n_residual_outliers) == (N, N, 0)
    assert s[on.id].median_residual_arcsec == pytest.approx(0.3, abs=1e-4)
    assert s[on.id].speed == pytest.approx(np.hypot(0.2, 0.05)) and s[on.id].sigma_v == pytest.approx(5e-5)
    assert s[on.id].median_mag == pytest.approx(np.median([22.0 + 0.1 * i for i in range(N)]))
    assert s[off.id].median_residual_arcsec == pytest.approx(0.0, abs=1e-4)
    assert s[off.id].n_residual_outliers == 4 and s[off.id].median_mag is None and s[off.id].sigma_v is None
    assert MAX_RESIDUAL_ARCSEC == 1.0
    assert (s[empty.id].n_gathered, s[empty.id].n_times, s[empty.id].n_residual_outliers) == (0, 0, 0)
    assert s[empty.id].median_residual_arcsec is None and s[empty.id].median_mag is None


def test_the_residual_is_against_the_tracklet_at_each_points_own_time(session):
    """A point on the track at its own time has zero residual even though the track moves; a point that is where
    the object *would be* at another time does not."""
    h.add_definition(session, "run")
    (r,) = h.add_run(session, "run", [{"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05}])
    h.add_gathered(session, r, [0.0] * N)
    compute_result_statistics(session)
    assert _stats(session)[r.id].median_residual_arcsec == pytest.approx(0.0, abs=1e-4)


def test_ra_wrap_is_handled(session):
    h.add_definition(session, "run")
    (r,) = h.add_run(session, "run", [{"ra": 0.0001, "dec": -13.0, "vra": -0.2, "vdec": 0.0}])  # crosses RA 0
    h.add_gathered(session, r, [0.0] * N, vra=-0.2, vdec=0.0, ra=0.0001)
    compute_result_statistics(session)
    assert _stats(session)[r.id].median_residual_arcsec == pytest.approx(0.0, abs=1e-3)


def test_idempotent_per_run_and_force_and_selection(session):
    h.add_definition(session, "run_a", detector=1)
    h.add_definition(session, "run_b", detector=2, night=20190403)
    (a,) = h.add_run(session, "run_a", [{"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05}])
    (b,) = h.add_run(session, "run_b", [{"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05}])
    h.add_gathered(session, a, [0.0] * N)
    h.add_gathered(session, b, [1.0] * N)
    assert compute_result_statistics(session, nights=[h.NIGHT]).runs == ["run_a"]
    again = compute_result_statistics(session)
    assert again.skipped_runs == ["run_a"] and again.runs == ["run_b"]
    assert len(_stats(session)) == 2
    assert compute_result_statistics(session, run_ids=["run_b"], force=True).runs == ["run_b"]
    assert len(_stats(session)) == 2 and compute_result_statistics(session, run_ids=["nope"]).runs == []


def test_cli(db_url):
    runner = CliRunner()
    with Session(create_db()) as session:
        h.add_definition(session, "run")
        (r,) = h.add_run(session, "run", [{"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05}])
        h.add_gathered(session, r, [0.0] * N)
        session.commit()
    out = runner.invoke(main, ["statistics"])
    assert out.exit_code == 0, out.output
    assert "1 run(s) computed, 0 skipped" in out.output and "1 result(s)" in out.output
    assert "0 run(s) computed, 1 skipped" in runner.invoke(main, ["statistics"]).output
    assert "1 run(s) computed" in runner.invoke(main, ["statistics", "--force"]).output
