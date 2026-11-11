import numpy as np
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from deep_asteroids.database import create_db, get_deep_db
from deep_asteroids.database.tracklets import KnownTracklet
from deep_asteroids.ingest.fit import predict, sep_arcsec, utc_to_tai
from deep_asteroids.ingest.tracklets import ingest_source

NIGHT = 20190402


@pytest.fixture
def session(db_url, deep_db_url):
    with Session(create_db()) as session:
        yield session


def _rows(session, source, **filters):
    q = select(KnownTracklet).where(KnownTracklet.source == source)
    for key, value in filters.items():
        q = q.where(getattr(KnownTracklet, key) == value)
    return session.scalars(q.order_by(KnownTracklet.id)).all()


def _deep_points(sql, **params):
    from sqlalchemy import text

    with get_deep_db().connect() as conn:
        return np.array(conn.execute(text(sql), params).fetchall(), dtype=float)


# --- synthetic ---------------------------------------------------------------

def test_synthetic_rows_per_component(session):
    summary = ingest_source(session, get_deep_db(), "synthetic", deep_db_sha="abc")
    assert summary.nights == [NIGHT]
    assert summary.n_points == 9
    rows = _rows(session, "synthetic")
    by_key = {(r.orbit_id, r.component): r for r in rows}
    assert set(by_key) == {
        (1001, "primary"),
        (1002, "primary"), (1002, "companion"), (1002, "barycenter"), (1002, "photocenter"),
    }
    for row in rows:
        assert row.night == NIGHT
        assert row.synthetic_source == "tno"
        assert row.flag is None
        assert row.n_points == 3
        assert row.deep_db_sha == "abc"
        assert row.max_residual_arcsec < 1e-3  # fixture points are exactly linear
    assert by_key[(1002, "primary")].deep_db_synthetic_object_id is not None
    assert by_key[(1002, "barycenter")].deep_db_synthetic_object_id is None


def test_synthetic_fit_reproduces_deep_db_points(session):
    ingest_source(session, get_deep_db(), "synthetic")
    (row,) = _rows(session, "synthetic", orbit_id=1001)
    pts = _deep_points(
        "SELECT e.midpoint_mjd, s.ra, s.dec FROM synthetic_object_ephemeris s "
        "JOIN exposure e ON e.id = s.exposure_id WHERE s.synthetic_object_id = :id",
        id=row.deep_db_synthetic_object_id,
    )
    t = utc_to_tai(pts[:, 0])
    ra, dec = predict(row.epoch, row.ra_epoch, row.dec_epoch, row.vra, row.vdec, t)
    assert sep_arcsec(ra, dec, pts[:, 1], pts[:, 2]).max() < 1e-3
    assert row.t_min == pytest.approx(t.min()) and row.t_max == pytest.approx(t.max())
    assert row.epoch == pytest.approx(t.mean())


def test_binary_barycenter_and_photocenter_positions(session):
    ingest_source(session, get_deep_db(), "synthetic")
    tracklets = {r.component: r for r in _rows(session, "synthetic", orbit_id=1002)}
    t = tracklets["primary"].epoch

    def at(component):
        r = tracklets[component]
        return predict(r.epoch, r.ra_epoch, r.dec_epoch, r.vra, r.vdec, t)

    primary, companion = at("primary"), at("companion")
    separation = sep_arcsec(*primary, *companion)
    assert separation == pytest.approx(2.0, rel=1e-3)  # fixture: companion 2" north
    bary, photo = at("barycenter"), at("photocenter")
    assert sep_arcsec(*primary, *bary) == pytest.approx(separation / 2, rel=1e-3)
    assert sep_arcsec(*companion, *bary) == pytest.approx(separation / 2, rel=1e-3)
    w = 10 ** (-0.4 * 0.5)  # fixture delta_h = 0.5
    assert sep_arcsec(*primary, *photo) == pytest.approx(separation * w / (1 + w), rel=1e-3)
    # photocenter lies between primary and barycenter (primary is brighter)
    assert sep_arcsec(*primary, *photo) < sep_arcsec(*primary, *bary)


# --- idempotency ---------------------------------------------------------------

def test_rerun_skips_and_force_replaces(session):
    deep = get_deep_db()
    first = ingest_source(session, deep, "synthetic")
    n = session.scalar(select(func.count()).select_from(KnownTracklet))
    ids = {r.id for r in _rows(session, "synthetic")}

    again = ingest_source(session, deep, "synthetic")
    assert again.nights == [] and again.skipped_nights == first.nights
    assert session.scalar(select(func.count()).select_from(KnownTracklet)) == n
    assert {r.id for r in _rows(session, "synthetic")} == ids

    # tamper with a row: a plain re-run must leave it alone, --force must replace it
    row = _rows(session, "synthetic", orbit_id=1001)[0]
    vra = row.vra
    row.vra = 99.0
    session.commit()
    ingest_source(session, deep, "synthetic")
    session.expire_all()
    assert _rows(session, "synthetic", orbit_id=1001)[0].vra == 99.0

    forced = ingest_source(session, deep, "synthetic", force=True)
    assert forced.nights == [NIGHT] and forced.n_written == n
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(KnownTracklet)) == n
    assert _rows(session, "synthetic", orbit_id=1001)[0].vra == pytest.approx(vra)


def test_nights_restricts_scope(session):
    summary = ingest_source(session, get_deep_db(), "synthetic", nights=[20200101])
    assert summary.nights == [20200101] and summary.n_written == 0
    assert _rows(session, "synthetic") == []


def test_unknown_source_raises(session):
    with pytest.raises(ValueError, match="unknown source"):
        ingest_source(session, get_deep_db(), "nope")


# --- mpc_observation -------------------------------------------------------------

def test_mpc_observation_rows_and_flags(session):
    summary = ingest_source(session, get_deep_db(), "mpc_observation", deep_db_sha="abc")
    assert summary.n_points == 3 + 3 + 4 + 1
    rows = {r.trkid: r for r in _rows(session, "mpc_observation")}
    assert set(rows) == {"00000E2QG2", "00000FFq5p", "00000CONF1", "00000SING1"}

    good = rows["00000E2QG2"]
    assert good.flag is None and good.n_points == 3
    assert good.trksub == "Hmdf1RN" and good.provid == "2019 GA179"
    assert good.statuses == "P"
    assert good.max_residual_arcsec < 1e-3

    assert rows["00000FFq5p"].flag == "conflated_trkid"
    assert rows["00000FFq5p"].provid is None  # deep-db leaves a conflated tracklet unattributed
    assert rows["00000CONF1"].flag == "same_obstime_conflict"
    assert rows["00000CONF1"].n_points == 4
    assert rows["00000SING1"].flag == "too_few_points"
    assert rows["00000SING1"].statuses == "p"
    for trkid in ("00000FFq5p", "00000CONF1", "00000SING1"):
        assert rows[trkid].vra is None and rows[trkid].max_residual_arcsec is None
    assert summary.n_flagged == {"conflated_trkid": 1, "same_obstime_conflict": 1, "too_few_points": 1}


def test_mpc_observation_fit_reproduces_observations(session):
    ingest_source(session, get_deep_db(), "mpc_observation")
    (row,) = _rows(session, "mpc_observation", trkid="00000E2QG2")
    pts = _deep_points(
        "SELECT julianday(obstime) - 2400000.5, ra, dec FROM mpc_observation WHERE mpc_tracklet_id = :id",
        id=row.deep_db_mpc_tracklet_id,
    )
    ra, dec = predict(row.epoch, row.ra_epoch, row.dec_epoch, row.vra, row.vdec, utc_to_tai(pts[:, 0]))
    assert sep_arcsec(ra, dec, pts[:, 1], pts[:, 2]).max() < 1e-3


def test_large_residual_flag_keeps_fit(session):
    # the fixture's points are exactly linear: a ~0 threshold flags every fitted tracklet
    summary = ingest_source(session, get_deep_db(), "mpc_observation", max_residual_arcsec=-1.0)
    rows = {r.trkid: r for r in _rows(session, "mpc_observation")}
    assert rows["00000E2QG2"].flag == "large_residual"
    assert rows["00000E2QG2"].vra is not None
    # no-fit flags win over large_residual
    assert rows["00000SING1"].flag == "too_few_points"
    assert summary.n_flagged["large_residual"] == 1
