import crossmatch_helpers as h
import numpy as np
import pytest
from sqlalchemy.orm import Session

from deep_asteroids.crossmatch.match import crossmatch_results
from deep_asteroids.crossmatch.uncertainty import (
    ResidualRow,
    bin_residuals_by_magnitude,
    median_mag_by_object,
    position_residuals,
    write_residuals_csv,
)
from deep_asteroids.database import create_db

V = {"vra": 0.2, "vdec": 0.05}


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


def _setup(session, offsets_arcsec, mags=None, permid="12345", east=0.0, north=0.0):
    """One run, one result, one numbered known object at ``east``/``north`` offset from it, with the
    result's gathered points offset north by ``offsets_arcsec`` from the object's own track."""
    h.add_definition(session, "run")
    ra, dec = h.offset(*h.BASE, east, north)
    (result,) = h.add_run(session, "run", [{"ra": ra, "dec": dec, **V}])
    h.add_gathered(session, result, offsets_arcsec, mags=mags, ra=ra, dec=dec, **V)
    known = h.add_known(session, "mpc_linked", *h.BASE, V["vra"], V["vdec"], permid=permid)
    crossmatch_results(session)
    return result, known


def test_residuals_are_measured_against_the_known_tracklets_own_prediction(session):
    offsets = [0.3] * h.N_EXPOSURES  # every gathered point 0.3" north of the object's own track
    _result, known = _setup(session, offsets)
    rows = position_residuals(session)
    assert len(rows) == h.N_EXPOSURES
    assert all(r.known_tracklet_id == known.id for r in rows)
    assert all(abs(r.dra_arcsec) < 1e-6 for r in rows)
    assert all(r.ddec_arcsec == pytest.approx(0.3, abs=1e-6) for r in rows)


def test_unnumbered_objects_are_excluded(session):
    h.add_definition(session, "run")
    (result,) = h.add_run(session, "run", [{"ra": h.BASE[0], "dec": h.BASE[1], **V}])
    h.add_gathered(session, result, [0.0] * h.N_EXPOSURES, ra=h.BASE[0], dec=h.BASE[1], **V)
    h.add_known(session, "mpc_linked", *h.BASE, V["vra"], V["vdec"], permid=None, provid="unnumbered")
    crossmatch_results(session)
    assert position_residuals(session) == []


def test_detections_beyond_the_detection_radius_are_dropped(session):
    # 10 of the 10 exposures start at 0.5" (inside the 1" default); push half out past 1"
    offsets = [0.5] * h.N_EXPOSURES
    offsets[0] = 1.5
    _result, known = _setup(session, offsets)
    rows = position_residuals(session, detection_radius_arcsec=1.0)
    assert len(rows) == h.N_EXPOSURES - 1
    assert all(r.ddec_arcsec == pytest.approx(0.5, abs=1e-6) for r in rows)


def test_match_radius_excludes_a_known_tracklet_too_far_from_any_result(session):
    _result, known = _setup(session, [0.0] * h.N_EXPOSURES, east=10.0)  # 10" away: beyond the 2" match radius
    assert position_residuals(session) == []


def test_median_mag_by_object_ignores_detections_with_no_magnitude(session):
    mags = [20.0, 21.0, 22.0] + [None] * (h.N_EXPOSURES - 3)
    _result, known = _setup(session, [0.0] * h.N_EXPOSURES, mags=mags)
    rows = position_residuals(session)
    by_object = median_mag_by_object(rows)
    assert by_object == {known.id: pytest.approx(21.0)}


def test_write_residuals_csv_round_trips(session, tmp_path):
    _result, known = _setup(session, [0.2] * h.N_EXPOSURES, mags=[21.0] * h.N_EXPOSURES)
    rows = position_residuals(session)
    out = write_residuals_csv(rows, tmp_path / "residuals.csv")
    lines = out.read_text().splitlines()
    assert lines[0] == "known_tracklet_id,result_id,night,permid,time,mag,dra_arcsec,ddec_arcsec"
    assert len(lines) == 1 + len(rows)
    assert str(known.id) in lines[1].split(",")


def test_night_filter(session):
    _result, known = _setup(session, [0.1] * h.N_EXPOSURES)
    assert position_residuals(session, nights=[h.NIGHT + 1]) == []
    assert len(position_residuals(session, nights=[h.NIGHT])) == h.N_EXPOSURES


def _row(kt_id, dra, ddec=0.0):
    return ResidualRow(known_tracklet_id=kt_id, result_id=0, night=h.NIGHT, permid="1", time=0.0, mag=None,
                       dra_arcsec=dra, ddec_arcsec=ddec)


def test_bin_residuals_by_magnitude_bins_on_the_objects_own_median_mag():
    # object 1 (mag 20): 25 detections with a mix of residuals -- median 0, MAD-sigma easy to hand-check
    bright = [_row(1, d) for d in [-0.1] * 12 + [0.1] * 12 + [0.0]]
    # object 2 (mag 22.5, same bin as a 0.5-wide bin starting at 22 would end at 22.5 -- place well inside the next bin)
    faint = [_row(2, d) for d in [-1.0] * 10 + [1.0] * 10 + [0.0]]
    rows = bright + faint
    by_object = {1: 20.0, 2: 22.5}
    bins = bin_residuals_by_magnitude(rows, by_object, bin_width=0.5, min_n=5)
    by_lo = {round(b[0], 2): b for b in bins}
    assert 20.0 in by_lo and 22.5 in by_lo
    lo, hi, n, median, sigma, median_dd, sigma_dd = by_lo[20.0]
    assert n == 25 and median == pytest.approx(0.0) and sigma == pytest.approx(0.1 * 1.4826)
    assert median_dd == pytest.approx(0.0) and sigma_dd == pytest.approx(0.0)
    *_, n2, median2, sigma2, _, _ = by_lo[22.5]
    assert n2 == 21 and median2 == pytest.approx(0.0) and sigma2 == pytest.approx(1.0 * 1.4826)


def test_bin_residuals_by_magnitude_drops_sparse_bins():
    rows = [_row(1, 0.0) for _ in range(3)]
    assert bin_residuals_by_magnitude(rows, {1: 20.0}, bin_width=0.5, min_n=5) == []


def test_bin_residuals_by_magnitude_skips_objects_with_no_magnitude():
    rows = [_row(1, 0.0)] * 10
    assert bin_residuals_by_magnitude(rows, {}, bin_width=0.5, min_n=5) == []
