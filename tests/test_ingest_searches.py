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


def test_one_band_search_and_one_undefined_search_per_detector(session):
    """Every exposure here is band 'VR', so each detector gets exactly two
    rows: the single-band one and the undefined-band (band=NULL) one
    spanning the same exposures."""
    summary = ingest_searches(session, get_deep_db(), deep_db_sha="abc")
    assert summary.nights == [NIGHT]
    assert summary.n_triples == 2
    assert summary.n_multi_band_triples == 0
    assert summary.n_searches == 4  # 2 detectors x (one band + one undefined)
    assert summary.n_exposures == 12  # 4 searches x 3 exposures each

    rows = _searches(session, night=NIGHT)
    assert [(r.detector_number, r.band) for r in rows] == [
        (4, "VR"), (4, None), (5, "VR"), (5, None),
    ]
    for row, detector_name in zip(rows, ("S25", "S25", "S26", "S26")):
        assert row.field == FIELD
        assert row.detector_name == detector_name
        base_run_id = f"deep-decam-{FIELD}-ccd{row.detector_number}-{NIGHT}"
        assert row.run_id == (base_run_id if row.band is None else f"{base_run_id}-{row.band}")
        assert row.n_exposures == 3
        assert row.t_start_mjd < row.t_end_mjd
        assert row.deep_db_sha == "abc"


def test_exposures_have_footprints_and_times(session):
    ingest_searches(session, get_deep_db())
    (row,) = _searches(session, night=NIGHT, detector_number=4, band=None)
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
    assert len(_searches(session)) == 4  # not duplicated


def test_force_replaces(session):
    ingest_searches(session, get_deep_db(), deep_db_sha="old")
    summary = ingest_searches(session, get_deep_db(), force=True, deep_db_sha="new")
    assert summary.nights == [NIGHT]
    rows = _searches(session)
    assert len(rows) == 4
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
    assert summary.n_searches == 4  # both detectors x (one band + one undefined) -- the other two exposures remain
    rows = _searches(session, night=NIGHT)
    assert all(r.n_exposures == 2 for r in rows)


def test_mixed_band_triple_gets_one_search_per_band_plus_undefined(session, deep_db_path, tmp_path, monkeypatch):
    """One exposure (shared by both detectors) changed to band 'r' --
    each detector is now a mixed-band triple: a 'VR' search (2 exposures),
    an 'r' search (1 exposure), and the undefined-band union (3 exposures)."""
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
        exposures = deep_session.scalars(select(Exposure).order_by(Exposure.id)).all()
        exposures[1].band = "r"
        deep_session.commit()

    monkeypatch.setenv("DEEP_DB", sqlite_readonly_url(copy_path))
    dispose_engines()
    deep_engine = get_deep_db()

    summary = ingest_searches(session, deep_engine)
    assert summary.n_triples == 2
    assert summary.n_multi_band_triples == 2
    assert summary.n_searches == 6  # 2 detectors x (VR + r + undefined)

    for detector_number in (4, 5):
        rows = {r.band: r for r in _searches(session, night=NIGHT, detector_number=detector_number)}
        assert set(rows) == {"VR", "r", None}
        assert rows["VR"].n_exposures == 2
        assert rows["r"].n_exposures == 1
        assert rows[None].n_exposures == 3
        assert rows["r"].run_id == f"deep-decam-{FIELD}-ccd{detector_number}-{NIGHT}-r"
