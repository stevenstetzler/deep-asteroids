import crossmatch_helpers as h
import numpy as np
import pytest
from click.testing import CliRunner
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.crossmatch.completeness import (
    bin_completeness,
    completeness_rows,
    inside_quadrilaterals,
    wilson,
)
from deep_asteroids.crossmatch.match import crossmatch_results
from deep_asteroids.database import create_db
from deep_asteroids.viz.plots import half_completeness_magnitude, plot_completeness

N = h.N_EXPOSURES
CHIP = (h.BASE[0], h.BASE[1], 0.1)  # a 0.2 deg square chip about the field centre, at every exposure
V = {"vra": 0.2, "vdec": 0.0}


def test_inside_quadrilaterals_on_a_rotated_chip_and_across_ra_zero():
    # a diamond (a square rotated 45 deg) centred on (10, 0), corners in a scrambled order, for two exposures
    corner_ra = np.array([[10.1, 9.9, 10.0, 10.0]] * 2)
    corner_dec = np.array([[0.0, 0.0, 0.1, -0.1]] * 2)
    ra = np.array([[10.0, 10.09], [10.06, 10.0], [10.0, 9.5]])
    dec = np.array([[0.0, 0.0], [0.06, 0.099], [0.0, 0.0]])
    inside = inside_quadrilaterals(ra, dec, corner_ra, corner_dec)
    assert inside.tolist() == [[True, True], [False, True], [True, False]]  # (10.06, 0.06) is outside the diamond

    # the same across RA = 0: wrapped coordinates must not matter
    wrap_ra = np.array([[0.05, 359.95, 0.0, 0.0]] * 2)
    wrap_dec = np.array([[0.0, 0.0, 0.05, -0.05]] * 2)
    pts_ra, pts_dec = np.array([[0.02, 359.98], [0.2, 359.8]]), np.array([[0.0, 0.0], [0.0, 0.0]])
    assert inside_quadrilaterals(pts_ra, pts_dec, wrap_ra, wrap_dec).tolist() == [[True, True], [False, False]]


def test_wilson_interval_contains_the_observed_fraction_and_narrows():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0 < hi < 0.2
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and 0.8 < lo < 1.0
    lo, hi = wilson(5, 10)
    assert lo < 0.5 < hi
    assert (wilson(50, 100)[1] - wilson(50, 100)[0]) < (wilson(5, 10)[1] - wilson(5, 10)[0])
    assert np.isnan(wilson(0, 0)[0])


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


SMALL_CHIP = (h.BASE[0], h.BASE[1], 0.03)  # 0.06 deg square: small enough that an object can leave it mid-night


def _scene(session):
    """One searched chip and eight synthetic objects:
      A  on the chip, mag 21.0, recovered           D  on the chip but speed 0.05: out of the velocity range
      B  on the chip, mag 22.0, recovered           E  leaves the chip: on it for 4 of 10 exposures, speed 0.45
      C  on the chip, mag 24.0, NOT recovered       F  nowhere near the searched chip
      G  a binary centre (not an object)            H  on the chip, no magnitude
    """
    h.add_definition(session, "run", footprint=SMALL_CHIP)
    h.add_run(session, "run", [{"ra": h.BASE[0], "dec": h.BASE[1], **V}, {"ra": h.BASE[0], "dec": h.BASE[1] + 0.01, **V}])
    ids = {}
    for name, kw in {
        "A": dict(mag=21.0), "B": dict(mag=22.0, dec_off=0.01), "C": dict(mag=24.0, dec_off=-0.02),
        "D": dict(mag=22.0, vra=0.05), "F": dict(mag=22.0, ra_off=5.0), "G": dict(mag=22.0, component="barycenter"),
        "H": dict(mag=None),
    }.items():
        k = h.add_known(session, "synthetic", h.BASE[0] + kw.get("ra_off", 0), h.BASE[1] + kw.get("dec_off", 0),
                        kw.get("vra", 0.2), 0.0, orbit_id=ord(name), component=kw.get("component", "primary"),
                        mag=kw["mag"])
        ids[name] = k.id
    # E moves 0.45 deg/day; at the middle exposure it is 0.037 deg (on the sky) right of centre, so it is inside
    # (u < 0.03) for exposures 0-3 and outside from the 4th on
    e_ra = h.BASE[0] + 0.037 / np.cos(np.radians(h.BASE[1]))
    ids["E"] = h.add_known(session, "synthetic", e_ra, h.BASE[1], 0.45, 0.0, orbit_id=ord("E"), component="primary",
                           mag=23.0).id
    session.commit()
    return ids


def test_findable_objects_and_recovery(session):
    ids = _scene(session)
    # the recovered ones: a crossmatch candidate within 2"
    from datetime import datetime

    from find_asteroids.models import Result

    from deep_asteroids.database.crossmatch import CrossmatchCandidate

    result = session.scalars(select(Result)).first()
    for name, sep in (("A", 0.1), ("B", 0.5), ("C", 5.0)):  # C only matched at 5": not recovered
        session.add(CrossmatchCandidate(
            result_id=result.id, known_tracklet_id=ids[name], night=h.NIGHT, source="synthetic", n_times=N,
            mean_sep_arcsec=sep, min_sep_arcsec=sep, max_sep_arcsec=sep, total_sep_arcsec=sep * N, velocity_diff=0.0,
            created_at=datetime(2026, 1, 1)))
    session.commit()

    rows = completeness_rows(session, nights=[h.NIGHT])
    by_id = {r.known_tracklet_id: r for r in rows}
    assert set(by_id) == {ids["A"], ids["B"], ids["C"]}  # D (speed), E (4 of 10 on chip), F, G, H are not findable
    assert [(by_id[ids[k]].mag, by_id[ids[k]].recovered) for k in "ABC"] == [(21.0, True), (22.0, True), (24.0, False)]
    assert all(r.run_id == "run" and r.n_on_chip == N and r.n_exposures == N for r in rows)

    # E is findable once only 4 of the 10 exposures need to be on the chip
    relaxed = completeness_rows(session, nights=[h.NIGHT], min_exposure_fraction=0.3)
    assert ids["E"] in {r.known_tracklet_id for r in relaxed} and ids["D"] not in {r.known_tracklet_id for r in relaxed}
    assert completeness_rows(session, nights=[20000101]) == [] and completeness_rows(session, run_ids=["nope"]) == []
    # a larger radius recovers C
    assert completeness_rows(session, radius=10.0)[2].recovered


def test_completeness_from_the_real_crossmatch_and_the_binned_curve(session):
    """The same, with the crossmatch run for real: a result sitting on a synthetic tracklet recovers it."""
    h.add_definition(session, "run", footprint=CHIP)
    h.add_run(session, "run", [{"ra": h.BASE[0], "dec": h.BASE[1], **V}])
    on = h.add_known(session, "synthetic", h.BASE[0], h.BASE[1], 0.2, 0.0, orbit_id=1, component="primary", mag=21.2)
    h.add_known(session, "synthetic", h.BASE[0], h.BASE[1] + 0.05, 0.2, 0.0, orbit_id=2, component="primary", mag=21.4)
    session.commit()
    crossmatch_results(session, sources=("synthetic",))
    rows = completeness_rows(session)
    assert min((r.known_tracklet_id, r.recovered) for r in rows) == (on.id, True)
    assert sum(r.recovered for r in rows) == 1 and len(rows) == 2
    (bin0,) = bin_completeness(rows, bin_width=1.0)
    assert bin0[:5] == (21.0, 22.0, 2, 1, 0.5) and bin0[5] < 0.5 < bin0[6]
    assert bin_completeness([]) == []


def test_half_completeness_magnitude_is_interpolated():
    bins = [(20.0, 21.0, 10, 10, 1.0, 0.9, 1.0), (21.0, 22.0, 10, 8, 0.8, 0.6, 0.9),
            (22.0, 23.0, 10, 2, 0.2, 0.1, 0.4), (23.0, 24.0, 10, 0, 0.0, 0.0, 0.1)]
    # bin centres 21.5 (0.8) and 22.5 (0.2) straddle one half: the crossing is a half of the way between them
    assert half_completeness_magnitude(bins) == pytest.approx(22.0)
    assert half_completeness_magnitude([(20.0, 21.0, 5, 5, 1.0, 0.8, 1.0)]) is None  # never falls below half
    assert half_completeness_magnitude([(20.0, 21.0, 5, 1, 0.2, 0.0, 0.5)]) is None  # never reaches half


def test_plot_and_cli(db_url, tmp_path):
    runner = CliRunner()
    with Session(create_db()) as session:
        h.add_definition(session, "run", footprint=CHIP)
        h.add_run(session, "run", [{"ra": h.BASE[0], "dec": h.BASE[1], **V}])
        h.add_known(session, "synthetic", h.BASE[0], h.BASE[1], 0.2, 0.0, orbit_id=1, component="primary", mag=21.2)
        h.add_known(session, "synthetic", h.BASE[0], h.BASE[1] + 0.05, 0.2, 0.0, orbit_id=2, component="primary",
                    mag=23.4)
        h.add_known(session, "mpchecker", h.BASE[0], h.BASE[1], 0.2, 0.0, provid="2000 AA", mag=19.0)
        session.commit()
    assert runner.invoke(main, ["crossmatch", "synthetic", "mpc-ephemeris"]).exit_code == 0

    out = runner.invoke(main, ["plot", "completeness", "--out", str(tmp_path / "c.png"), "--night", str(h.NIGHT)])
    assert out.exit_code == 0, out.output
    assert "2 findable synthetic objects, 1 recovered" in out.output
    assert (tmp_path / "c.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    out = runner.invoke(main, ["plot", "completeness", "--out", str(tmp_path / "k.png"), "--source", "mpchecker"])
    assert out.exit_code == 0 and "1 findable mpchecker objects, 1 recovered" in out.output
    none = runner.invoke(main, ["plot", "completeness", "--out", str(tmp_path / "x.png"), "--night", "19000101"])
    assert none.exit_code != 0 and "no findable synthetic objects" in none.output

    from deep_asteroids.crossmatch.completeness import CompletenessRow

    rows = [CompletenessRow(1, h.NIGHT, 21.0, 0.2, "r", 10, 10, True), CompletenessRow(2, h.NIGHT, 24.0, 0.2, "r", 10, 10, False)]
    assert plot_completeness(rows, tmp_path / "direct.png", radius=2.0).exists()
    with pytest.raises(ValueError, match="no findable"):
        plot_completeness([], tmp_path / "empty.png")
