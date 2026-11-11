"""
Shared fixtures: a small, fully synthetic deep-db and an empty
deep-asteroids database, both temporary SQLite files. No network, no
Postgres.

The deep-db fixture is built from deep-db's own models (so it tracks the
pinned deep-db schema exactly) and seeded with one night of known
geometry and one object of each kind phase 1 ingests: an MPC observation
tracklet, an mpchecker ephemeris object, a single fake, and a fake
binary (primary + companion).
"""
from datetime import datetime, timedelta

import pytest

NIGHT = 20190402
FIELD = "A0b"
# three 120 s exposures, ~15 min apart -- a short but fittable baseline
EXPOSURE_START = datetime(2019, 4, 3, 5, 0, 0)
EXPOSURE_CADENCE = timedelta(minutes=15)
EXPOSURE_TIME = 120.0
N_EXPOSURES = 3
BORESIGHT = (215.0, -13.0)
DETECTORS = ((4, "S25"), (5, "S26"))


def _mjd(dt):
    from astropy.time import Time

    return float(Time(dt, scale="utc").mjd)


def _seed_deep_db(session):
    from deep_db.database.mpc import MPCObject, MPCObservation, MPCTracklet
    from deep_db.database.mpchecker import MPCheckerEphemeris
    from deep_db.database.survey import Detector, DetectorExposure, Exposure, Field, Night
    from deep_db.database.synthetic_objects import (
        SyntheticObject,
        SyntheticObjectBinary,
        SyntheticObjectDesignation,
        SyntheticObjectEphemeris,
    )

    night = Night(
        night=NIGHT, start_time=EXPOSURE_START,
        end_time=EXPOSURE_START + (N_EXPOSURES - 1) * EXPOSURE_CADENCE + timedelta(seconds=EXPOSURE_TIME),
    )
    field = Field(name=FIELD)
    detectors = [Detector(number=n, name=name) for n, name in DETECTORS]
    session.add_all([night, field, *detectors])
    session.flush()

    exposures, detector_exposures = [], {}
    for i in range(N_EXPOSURES):
        obstime = EXPOSURE_START + i * EXPOSURE_CADENCE
        midpoint = obstime + timedelta(seconds=EXPOSURE_TIME / 2)
        exposure = Exposure(
            night_id=night.id, field_id=field.id, expnum=845580 + i, caldat="2019-04-02",
            exposure_time=EXPOSURE_TIME, obstime=obstime, mjd=_mjd(obstime),
            midpoint=midpoint, midpoint_mjd=_mjd(midpoint), band="VR", target=FIELD,
            ra=BORESIGHT[0], dec=BORESIGHT[1],
        )
        session.add(exposure)
        session.flush()
        exposures.append(exposure)
        for j, detector in enumerate(detectors):
            # two adjacent 0.3 deg x 0.15 deg chips; geometry only needs to be plausible here
            ra0 = BORESIGHT[0] - 0.3 + 0.3 * j
            de = DetectorExposure(
                detector_id=detector.id, exposure_id=exposure.id,
                ra=ra0 + 0.15, dec=BORESIGHT[1] + 0.075,
                ra_00=ra0, dec_00=BORESIGHT[1], ra_01=ra0 + 0.3, dec_01=BORESIGHT[1],
                ra_10=ra0, dec_10=BORESIGHT[1] + 0.15, ra_11=ra0 + 0.3, dec_11=BORESIGHT[1] + 0.15,
            )
            session.add(de)
            session.flush()
            detector_exposures[(i, detector.number)] = de

    def track(ra, dec, vra, vdec, i):
        """Linear track (deg, deg/day) evaluated at exposure i's midpoint."""
        dt = (exposures[i].midpoint_mjd - exposures[0].midpoint_mjd)
        return ra + vra * dt, dec + vdec * dt

    # an MPC observation tracklet, attributed to one designated object
    mpc_object = MPCObject(provid="2019 GA179")
    session.add(mpc_object)
    session.flush()
    tracklet = MPCTracklet(object_id=mpc_object.id, trkid="00000E2QG2", trksub="Hmdf1RN")
    session.add(tracklet)
    session.flush()
    for i in range(N_EXPOSURES):
        ra, dec = track(214.9, -12.95, 0.2, -0.05, i)
        session.add(MPCObservation(
            obsid=f"obs{i}", object_id=mpc_object.id, exposure_id=exposures[i].id,
            detector_exposure_id=detector_exposures[(i, 4)].id, mpc_tracklet_id=tracklet.id,
            ra=ra, dec=dec, mag=23.1, obstime=exposures[i].midpoint, status="P",
        ))
        # mpchecker's prediction for the same object (off-chip on the last exposure)
        session.add(MPCheckerEphemeris(
            object_id=mpc_object.id, exposure_id=exposures[i].id,
            detector_exposure_id=detector_exposures[(i, 4)].id if i < N_EXPOSURES - 1 else None,
            ra=ra, dec=dec, v_mag=23.0,
        ))

    # three problem MPC tracklets (all on detector 4):
    # - conflated: observations attributed to two different objects, ~1 deg apart
    other_object = MPCObject(provid="2020 UQ54")
    session.add(other_object)
    session.flush()
    conflated = MPCTracklet(object_id=None, trkid="00000FFq5p", trksub="IASp1Nm")
    # - same-obstime conflict: two observations at the same time, 10" apart
    conflict = MPCTracklet(object_id=None, trkid="00000CONF1", trksub="conf1")
    # - single observation: nothing to fit
    single_obs = MPCTracklet(object_id=None, trkid="00000SING1", trksub="sing1")
    session.add_all([conflated, conflict, single_obs])
    session.flush()
    for i in range(N_EXPOSURES):
        de_id = detector_exposures[(i, 4)].id
        obj = mpc_object if i < 2 else other_object
        ra, dec = track(214.8 + (1.0 if i == 2 else 0.0), -12.9, 0.2, 0.0, i)
        session.add(MPCObservation(obsid=f"conflated{i}", object_id=obj.id, exposure_id=exposures[i].id,
                                   detector_exposure_id=de_id, mpc_tracklet_id=conflated.id,
                                   ra=ra, dec=dec, obstime=exposures[i].midpoint, status="I"))
        ra, dec = track(214.7, -12.8, 0.1, 0.0, i)
        session.add(MPCObservation(obsid=f"conflict{i}", object_id=None, exposure_id=exposures[i].id,
                                   detector_exposure_id=de_id, mpc_tracklet_id=conflict.id,
                                   ra=ra, dec=dec, obstime=exposures[i].midpoint, status="I"))
        if i == 1:
            session.add(MPCObservation(obsid="conflict1b", object_id=None, exposure_id=exposures[i].id,
                                       detector_exposure_id=de_id, mpc_tracklet_id=conflict.id,
                                       ra=ra, dec=dec + 10.0 / 3600, obstime=exposures[i].midpoint, status="I"))
    session.add(MPCObservation(obsid="single0", object_id=None, exposure_id=exposures[0].id,
                               detector_exposure_id=None, mpc_tracklet_id=single_obs.id,
                               ra=214.6, dec=-12.7, obstime=exposures[0].midpoint, status="p"))

    # one single fake and one fake binary (primary + companion, 2" apart)
    single_designation = SyntheticObjectDesignation(orbit_id=1001, name="fake1001")
    binary_designation = SyntheticObjectDesignation(orbit_id=1002, name="fake1002")
    session.add_all([single_designation, binary_designation])
    session.flush()
    single = SyntheticObject(designation_id=single_designation.id, source="tno", orbit_id=1001, component="primary")
    primary = SyntheticObject(designation_id=binary_designation.id, source="tno", orbit_id=1002, component="primary")
    companion = SyntheticObject(designation_id=binary_designation.id, source="tno", orbit_id=1002, component="companion")
    session.add_all([single, primary, companion])
    session.flush()
    session.add(SyntheticObjectBinary(
        primary_synthetic_object_id=primary.id, companion_synthetic_object_id=companion.id,
        delta_h=0.5, separation=2.0, angle=90.0,
    ))
    for i in range(N_EXPOSURES):
        de_id = detector_exposures[(i, 5)].id
        ra, dec = track(215.1, -12.9, 0.01, 0.002, i)
        session.add(SyntheticObjectEphemeris(synthetic_object_id=single.id, exposure_id=exposures[i].id,
                                             detector_exposure_id=de_id, ra=ra, dec=dec, mag=22.0))
        ra, dec = track(215.2, -12.92, 0.012, 0.001, i)
        session.add(SyntheticObjectEphemeris(synthetic_object_id=primary.id, exposure_id=exposures[i].id,
                                             detector_exposure_id=de_id, ra=ra, dec=dec, mag=23.0))
        session.add(SyntheticObjectEphemeris(synthetic_object_id=companion.id, exposure_id=exposures[i].id,
                                             detector_exposure_id=de_id, ra=ra, dec=dec + 2.0 / 3600, mag=23.5))
    session.commit()


@pytest.fixture(scope="session")
def deep_db_path(tmp_path_factory):
    """Path to a seeded, synthetic deep-db SQLite file (built once per test session)."""
    import deep_db.database.mpc
    import deep_db.database.mpchecker
    import deep_db.database.survey
    import deep_db.database.synthetic_objects  # noqa: F401
    from deep_db.database.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    path = tmp_path_factory.mktemp("deep_db") / "deep_database.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_deep_db(session)
    engine.dispose()
    return path


@pytest.fixture
def deep_db_url(deep_db_path, monkeypatch):
    """$DEEP_DB pointed (read-only, immutable) at the seeded deep-db fixture."""
    from deep_asteroids.util.deep_db import sqlite_readonly_url

    url = sqlite_readonly_url(deep_db_path)
    monkeypatch.setenv("DEEP_DB", url)
    return url


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    """$DEEP_ASTEROIDS_DB pointed at a fresh, empty SQLite file; engines disposed afterwards."""
    from deep_asteroids.database import dispose_engines

    url = f"sqlite:///{tmp_path / 'deep_asteroids.sqlite'}"
    monkeypatch.setenv("DEEP_ASTEROIDS_DB", url)
    yield url
    dispose_engines()
