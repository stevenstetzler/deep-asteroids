import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.database import create_db, get_deep_db
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure
from deep_asteroids.ingest.searches import ingest_searches

NIGHT = 20190402
FIELD = "A0b"


@pytest.fixture
def session(db_url, deep_db_url):
    with Session(create_db()) as session:
        yield session


def _searches(session, **filters):
    q = select(SearchDefinition)
    for key, value in filters.items():
        q = q.where(getattr(SearchDefinition, key) == value)
    return session.scalars(q.order_by(SearchDefinition.detector_number)).all()


def test_one_search_per_detector(session):
    summary = ingest_searches(session, get_deep_db(), deep_db_sha="abc")
    assert summary.nights == [NIGHT]
    assert summary.n_searches == 2
    assert summary.n_exposures == 6
    assert summary.n_mixed_band == 0

    rows = _searches(session, night=NIGHT)
    assert [r.detector_number for r in rows] == [4, 5]
    for row, detector_name in zip(rows, ("S25", "S26")):
        assert row.field == FIELD
        assert row.detector_name == detector_name
        assert row.run_id == f"{FIELD}-ccd{row.detector_number:02d}-{NIGHT}"
        assert row.band == "VR"
        assert row.n_exposures == 3
        assert row.t_start_mjd < row.t_end_mjd
        assert row.deep_db_sha == "abc"


def test_exposures_have_footprints_and_times(session):
    ingest_searches(session, get_deep_db())
    (row,) = _searches(session, night=NIGHT, detector_number=4)
    exposures = session.scalars(
        select(SearchDefinitionExposure)
        .where(SearchDefinitionExposure.search_definition_id == row.id)
        .order_by(SearchDefinitionExposure.midpoint_mjd)
    ).all()
    assert len(exposures) == 3
    mjds = [e.midpoint_mjd for e in exposures]
    assert mjds == sorted(mjds)
    assert row.t_start_mjd == mjds[0]
    assert row.t_end_mjd == mjds[-1]
    for e in exposures:
        assert e.ra_00 < e.ra_01  # detector 4's chip spans [boresight-0.3, boresight)
        assert e.dec_00 < e.dec_10
        assert e.expnum is not None
        assert e.deep_db_exposure_id is not None
        assert e.deep_db_detector_exposure_id is not None


def test_rerun_skips_existing_night(session):
    ingest_searches(session, get_deep_db())
    summary = ingest_searches(session, get_deep_db())
    assert summary.nights == []
    assert summary.skipped_nights == [NIGHT]
    assert len(_searches(session)) == 2  # not duplicated


def test_force_replaces(session):
    ingest_searches(session, get_deep_db(), deep_db_sha="old")
    summary = ingest_searches(session, get_deep_db(), force=True, deep_db_sha="new")
    assert summary.nights == [NIGHT]
    rows = _searches(session)
    assert len(rows) == 2
    assert all(r.deep_db_sha == "new" for r in rows)


def test_night_filter_with_no_matching_data(session):
    summary = ingest_searches(session, get_deep_db(), nights=[20200101])
    assert summary.nights == [20200101]
    assert summary.n_searches == 0
    assert _searches(session) == []


def test_exposure_with_no_field_is_excluded(session, deep_db_path, tmp_path, monkeypatch):
    """A copy of the fixture deep-db, mutated and reopened read-only --
    the shared session-scoped `deep_db_path` fixture is never written to,
    so this can't leak into other tests."""
    import shutil

    from deep_db.database.survey import Exposure
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    from deep_asteroids.database import dispose_engines
    from deep_asteroids.util.deep_db import sqlite_readonly_url

    copy_path = tmp_path / "deep_database.sqlite"
    shutil.copy(deep_db_path, copy_path)
    writable_url = make_url(f"sqlite:///{copy_path}")
    with Session(create_engine(writable_url)) as deep_session:
        exposure = deep_session.scalars(select(Exposure).order_by(Exposure.id)).first()
        exposure.field_id = None
        deep_session.commit()

    monkeypatch.setenv("DEEP_DB", sqlite_readonly_url(copy_path))
    dispose_engines()
    deep_engine = get_deep_db()

    summary = ingest_searches(session, deep_engine)
    assert summary.n_searches == 2  # still both detectors -- the other two exposures remain
    rows = _searches(session, night=NIGHT)
    assert all(r.n_exposures == 2 for r in rows)
