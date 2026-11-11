from datetime import datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from deep_asteroids.database import create_db
from deep_asteroids.database.tracklets import KnownTracklet

FIT = {"epoch": 58600.5, "ra_epoch": 215.0, "dec_epoch": -13.0, "vra": 0.25, "vdec": -0.05}


def _row(**kwargs):
    defaults = {
        "source": "synthetic", "night": 20190402, "t_min": 58600.4, "t_max": 58600.6, "n_points": 3,
        "synthetic_source": "tno", "orbit_id": 1, "component": "primary", "created_at": datetime(2026, 11, 11),
        **FIT,
    }
    return KnownTracklet(**{**defaults, **kwargs})


@pytest.fixture
def session(db_url):
    engine = create_db()
    with Session(engine) as session:
        yield session


def test_create_db_creates_known_tracklet(session):
    assert "known_tracklet" in inspect(session.get_bind()).get_table_names()


def test_ra_0_dec_0_follow_find_asteroids_convention():
    row = _row()
    assert row.ra_0 == pytest.approx(215.0 - 0.25 * 58600.5)
    assert row.dec_0 == pytest.approx(-13.0 + 0.05 * 58600.5)
    assert _row(flag="too_few_points", **dict.fromkeys(FIT)).ra_0 is None


def test_natural_key_is_unique_per_source(session):
    session.add(_row())
    session.commit()
    session.add(_row())
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    # same orbit_id on another night, or another component, is fine
    session.add_all([_row(night=20190403), _row(component="barycenter")])
    session.commit()


def test_identity_keys_of_different_sources_do_not_collide(session):
    session.add_all([
        _row(source="mpc_observation", deep_db_mpc_tracklet_id=7, orbit_id=None, component=None,
             synthetic_source=None),
        _row(source="mpchecker", deep_db_mpc_object_id=7, orbit_id=None, component=None, synthetic_source=None),
    ])
    session.commit()
    session.add(_row(source="mpchecker", deep_db_mpc_object_id=7, orbit_id=None, component=None,
                     synthetic_source=None))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize("bad", [
    {"source": "nope"},
    {"component": "nope"},
    {"flag": "nope"},
    {"flag": "too_few_points"},  # flagged but has a fit
    {"vra": None},  # partial fit, unflagged
])
def test_check_constraints(session, bad):
    session.add(_row(**bad))
    with pytest.raises(IntegrityError):
        session.commit()


def test_flagged_row_without_fit_is_allowed(session):
    session.add(_row(flag="too_few_points", max_residual_arcsec=None, **dict.fromkeys(FIT)))
    session.commit()
