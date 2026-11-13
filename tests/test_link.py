"""Tests for ``ingest/link.py``: a tiny deep-db with MPC tracklets split, conflicting, crossing
and unfittable, through ``ingest tracklets mpc-observations`` and then ``link``."""
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.database.crossmatch import CrossmatchRun
from deep_asteroids.database.tracklets import KnownTracklet, KnownTrackletMember
from deep_asteroids.ingest.link import link_mpc_tracklets
from deep_asteroids.ingest.tracklets import ingest_source

NIGHT = 20190402
N = 8  # exposures
START = datetime(2019, 4, 3, 5, 0, 0)
CADENCE = timedelta(minutes=15)
BASE = (214.9, -12.95)
VRA, VDEC = 0.2, -0.05  # deg/day


def _mjd(dt):
    from astropy.time import Time

    return float(Time(dt, scale="utc").mjd)


@pytest.fixture
def deep_engine(tmp_path):
    """A deep-db holding, on one night of 8 exposures:
      S1+S2   one undesignated object split in halves (exposures 0-3 and 4-7)
      D1+D2   one designated object (2019 AA1) split in halves
      X1, X2  two different designated objects on the same track
      Y       an object crossing S's track at one exposure, moving differently
      FAR     an object far from everything
      ONE     a single observation: nothing to fit
    """
    import deep_db.database.mpc
    import deep_db.database.mpchecker
    import deep_db.database.survey
    import deep_db.database.synthetic_objects  # noqa: F401
    from deep_db.database.models import Base
    from deep_db.database.mpc import MPCObject, MPCObservation, MPCTracklet
    from deep_db.database.survey import Exposure, Field, Night

    path = tmp_path / "deep.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        night = Night(night=NIGHT, start_time=START, end_time=START + N * CADENCE)
        field = Field(name="A0b")
        session.add_all([night, field])
        session.flush()
        exposures = []
        for i in range(N):
            obstime = START + i * CADENCE
            midpoint = obstime + timedelta(seconds=60)
            e = Exposure(night_id=night.id, field_id=field.id, expnum=800 + i, caldat="2019-04-02", exposure_time=120.0,
                         obstime=obstime, mjd=_mjd(obstime), midpoint=midpoint, midpoint_mjd=_mjd(midpoint),
                         band="VR", target="A0b", ra=BASE[0], dec=BASE[1])
            session.add(e)
            exposures.append(e)
        session.flush()

        def track(ra, dec, vra, vdec, i):
            dt = exposures[i].midpoint_mjd - exposures[0].midpoint_mjd
            return ra + vra * dt, dec + vdec * dt

        objects = {}

        def obj(provid):
            if provid and provid not in objects:
                objects[provid] = MPCObject(provid=provid)
                session.add(objects[provid])
                session.flush()
            return objects.get(provid)

        def tracklet(trkid, provid, indexes, ra, dec, vra, vdec, status="I"):
            o = obj(provid)
            t = MPCTracklet(object_id=o.id if o else None, trkid=trkid, trksub=trkid.lower())
            session.add(t)
            session.flush()
            for i in indexes:
                r, d = track(ra, dec, vra, vdec, i)
                session.add(MPCObservation(obsid=f"{trkid}-{i}", object_id=o.id if o else None, exposure_id=exposures[i].id,
                                           mpc_tracklet_id=t.id, ra=r, dec=d, obstime=exposures[i].midpoint,
                                           status=status))

        first, second = range(4), range(4, 8)
        tracklet("S1", None, first, *BASE, VRA, VDEC, "I")
        tracklet("S2", None, second, *BASE, VRA, VDEC, "I")
        tracklet("D1", "2019 AA1", first, BASE[0], BASE[1] + 60 / 3600, VRA, VDEC, "P")
        tracklet("D2", "2019 AA1", second, BASE[0], BASE[1] + 60 / 3600, VRA, VDEC, "P")
        tracklet("X1", "2019 BB1", range(N), BASE[0], BASE[1] - 60 / 3600, VRA, VDEC, "P")
        tracklet("X2", "2019 BB2", range(N), BASE[0], BASE[1] - 60.2 / 3600, VRA, VDEC, "P")
        # crosses S at exposure 3 (it is at BASE moved by S's velocity), with a different velocity
        cross_ra, cross_dec = track(*BASE, VRA, VDEC, 3)
        dt3 = exposures[3].midpoint_mjd - exposures[0].midpoint_mjd
        tracklet("Y", None, range(N), cross_ra - 0.3 * dt3, cross_dec + 0.1 * dt3, 0.3, -0.1, "I")
        tracklet("FAR", None, range(N), BASE[0] + 1.0, BASE[1] + 1.0, VRA, VDEC, "p")
        tracklet("ONE", None, [0], BASE[0] - 1.0, BASE[1], 0, 0, "I")
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def session(db_url, deep_engine):
    with Session(create_db()) as session:
        ingest_source(session, deep_engine, "mpc_observation")
        yield session


def _linked(session):
    return session.scalars(select(KnownTracklet).where(KnownTracklet.source == "mpc_linked")
                           .order_by(KnownTracklet.id)).all()


def _members(session, linked):
    rows = session.execute(
        select(KnownTracklet.trkid).join(KnownTrackletMember, KnownTrackletMember.member_id == KnownTracklet.id)
        .where(KnownTrackletMember.linked_id == linked.id)).scalars().all()
    return sorted(rows)


def _by_members(session):
    return {tuple(_members(session, k)): k for k in _linked(session)}


def test_links_split_tracklets_and_keeps_distinct_ones_apart(session, deep_engine):
    summary = link_mpc_tracklets(session, deep_engine, [NIGHT])
    groups = _by_members(session)
    assert set(groups) == {("S1", "S2"), ("D1", "D2"), ("X1", "X2"), ("Y",), ("FAR",)}  # ONE has no fit
    assert summary.nights == [NIGHT] and summary.n_members == 8 and summary.n_groups == 5
    assert summary.n_merged_away == 3 and summary.group_sizes == {2: 3, 1: 2} and summary.n_unlinked == 1


def test_merged_fit_covers_the_whole_night_and_all_the_observations(session, deep_engine):
    link_mpc_tracklets(session, deep_engine, [NIGHT])
    k = _by_members(session)[("S1", "S2")]
    assert (k.source, k.night, k.n_members, k.n_points, k.flag) == ("mpc_linked", NIGHT, 2, 8, None)
    assert k.vra == pytest.approx(VRA, abs=1e-6) and k.vdec == pytest.approx(VDEC, abs=1e-6)
    parts = [r for r in session.scalars(select(KnownTracklet).where(KnownTracklet.source == "mpc_observation"))
             if r.trkid in ("S1", "S2")]
    assert k.t_min == min(p.t_min for p in parts) and k.t_max == max(p.t_max for p in parts)
    assert k.t_max - k.t_min > max(p.t_max - p.t_min for p in parts)  # a longer baseline than either half
    assert k.max_residual_arcsec < 1e-3 and k.statuses == "I"
    assert k.deep_db_mpc_tracklet_id is None and k.trkid is None


def test_a_singleton_keeps_its_own_fit(session, deep_engine):
    link_mpc_tracklets(session, deep_engine, [NIGHT])
    k = _by_members(session)[("FAR",)]
    (own,) = [r for r in session.scalars(select(KnownTracklet).where(KnownTracklet.source == "mpc_observation"))
              if r.trkid == "FAR"]
    assert (k.n_members, k.epoch, k.ra_epoch, k.vra, k.n_points, k.statuses) == (
        1, own.epoch, own.ra_epoch, own.vra, own.n_points, "p")


def test_agreement_is_on_average_not_at_one_exposure(session, deep_engine):
    """Y crosses S's track at one exposure: close there, far at the others, so not linked."""
    link_mpc_tracklets(session, deep_engine, [NIGHT])
    assert ("Y",) in _by_members(session) and ("S1", "S2", "Y") not in _by_members(session)


def test_designations_are_carried_and_conflicts_reported(session, deep_engine):
    link_mpc_tracklets(session, deep_engine, [NIGHT])
    groups = _by_members(session)
    d = groups[("D1", "D2")]
    assert (d.n_designations, d.designations, d.provid, d.statuses) == (1, "2019 AA1", "2019 AA1", "P")
    x = groups[("X1", "X2")]
    assert (x.n_designations, x.designations, x.provid) == (2, "2019 BB1,2019 BB2", None)  # reported, not split
    s = groups[("S1", "S2")]
    assert (s.n_designations, s.designations, s.provid) == (0, None, None)
    assert link_mpc_tracklets(session, deep_engine, [NIGHT], force=True).n_conflicting_designations == 1


def test_membership_ties_a_linked_row_back_to_its_trkids(session, deep_engine):
    link_mpc_tracklets(session, deep_engine, [NIGHT])
    for members, linked in _by_members(session).items():
        rows = session.scalars(select(KnownTrackletMember).where(KnownTrackletMember.linked_id == linked.id)).all()
        assert len(rows) == len(members) == linked.n_members
    originals = session.scalars(select(KnownTracklet.id).where(
        KnownTracklet.source == "mpc_observation", KnownTracklet.epoch.is_not(None))).all()
    members = session.scalars(select(KnownTrackletMember.member_id)).all()
    assert sorted(members) == sorted(originals)  # every fitted tracklet is in exactly one group


def test_idempotent_and_force_replaces(session, deep_engine):
    first = link_mpc_tracklets(session, deep_engine, [NIGHT])
    ids = [k.id for k in _linked(session)]
    again = link_mpc_tracklets(session, deep_engine, [NIGHT])
    assert again.skipped_nights == [NIGHT] and not again.nights and [k.id for k in _linked(session)] == ids
    forced = link_mpc_tracklets(session, deep_engine, [NIGHT], force=True)
    assert forced.nights == [NIGHT] and forced.n_groups == first.n_groups == len(_linked(session))
    assert len(session.scalars(select(KnownTrackletMember)).all()) == first.n_members
    assert link_mpc_tracklets(session, deep_engine, [20000101]).nights == []  # nothing ingested there


def test_a_tighter_radius_links_less(session, deep_engine):
    link_mpc_tracklets(session, deep_engine, [NIGHT], match_radius_arcsec=0.05)
    assert ("X1", "X2") not in _by_members(session) and ("S1", "S2") in _by_members(session)


def test_replacing_the_per_trkid_rows_or_linking_invalidates_the_crossmatch(session, deep_engine):
    from datetime import datetime as dt

    link_mpc_tracklets(session, deep_engine, [NIGHT])
    session.add(CrossmatchRun(run_id="r", night=NIGHT, sources="mpc_linked", max_sep_arcsec=30.0, n_results=1,
                              n_known_tracklets=5, n_candidates=0, created_at=dt(2026, 1, 1)))
    session.commit()
    assert "invalidated" in link_mpc_tracklets(session, deep_engine, [NIGHT], force=True).format()
    assert session.scalars(select(CrossmatchRun)).all() == []

    session.add(CrossmatchRun(run_id="r", night=NIGHT, sources="mpc_linked", max_sep_arcsec=30.0, n_results=1,
                              n_known_tracklets=5, n_candidates=0, created_at=dt(2026, 1, 1)))
    session.commit()
    ingest_source(session, deep_engine, "mpc_observation", force=True)  # per-trkid rows replaced: linked ones go too
    assert _linked(session) == [] and session.scalars(select(KnownTrackletMember)).all() == []
    assert session.scalars(select(CrossmatchRun)).all() == []


def test_cli(db_url, deep_engine, monkeypatch):
    from deep_asteroids.util.deep_db import sqlite_readonly_url

    monkeypatch.setenv("DEEP_DB", sqlite_readonly_url(deep_engine.url.database))
    runner = CliRunner()
    nothing = runner.invoke(main, ["link"])
    assert nothing.exit_code != 0 and "no MPC tracklets ingested" in nothing.output
    assert runner.invoke(main, ["ingest", "tracklets", "mpc-observations"]).exit_code == 0
    out = runner.invoke(main, ["link", "--night", str(NIGHT)])
    assert out.exit_code == 0, out.output
    assert "8 fitted MPC tracklet(s) -> 5 group(s); 3 merged into another" in out.output
    assert "1 group(s) join more than one designation" in out.output
    assert "0 night(s) linked, 1 skipped" in runner.invoke(main, ["link"]).output
    assert "1 night(s) linked" in runner.invoke(main, ["link", "--force"]).output

    sources = runner.invoke(main, ["query", "tracklets", "source", "n_members", "--source", "mpc_linked"])
    assert sorted(sources.output.split()) == ["mpc_linked|1"] * 2 + ["mpc_linked|2"] * 3


def test_visualize_linked_writes_a_figure_per_merged_group(session, deep_engine, tmp_path):
    from deep_asteroids.viz.linked import load_groups, visualize_linked

    link_mpc_tracklets(session, deep_engine, [NIGHT])
    groups = load_groups(session, NIGHT)
    assert sorted(tuple(m["trkid"] for m in g.members) for g in groups) == [("D1", "D2"), ("S1", "S2"), ("X1", "X2")]
    assert [g.linked["n_members"] for g in groups] == [2, 2, 2] and all(g.linked["night"] == NIGHT for g in groups)

    summary = visualize_linked(session, deep_engine, NIGHT, tmp_path)
    figures = [p for p in summary.paths if not p.name.startswith("contact")]
    assert summary.n_groups == 3 and len(figures) == 3 and "3 figure(s)" in summary.format()
    assert all(p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and p.stat().st_size > 10_000 for p in summary.paths)
    assert [p.name for p in summary.paths if p.name.startswith("contact")] == ["contact_sheet_01.png"]

    one = visualize_linked(session, deep_engine, NIGHT, tmp_path / "one", linked_ids=[groups[0].linked["id"]],
                           sheets=False)
    assert one.n_groups == 1 and len(list((tmp_path / "one").iterdir())) == 1
    assert visualize_linked(session, deep_engine, NIGHT, tmp_path / "none", min_members=3).n_groups == 0
    assert visualize_linked(session, deep_engine, NIGHT, tmp_path / "lim", limit=2, sheets=False).n_groups == 2


def test_viz_linked_cli(db_url, deep_engine, monkeypatch, tmp_path):
    from deep_asteroids.util.deep_db import sqlite_readonly_url

    monkeypatch.setenv("DEEP_DB", sqlite_readonly_url(deep_engine.url.database))
    runner = CliRunner()
    assert runner.invoke(main, ["ingest", "tracklets", "mpc-observations"]).exit_code == 0
    out = runner.invoke(main, ["viz", "linked", "--night", str(NIGHT), "--out-dir", str(tmp_path / "v")])
    assert out.exit_code != 0 and "no linked tracklet" in out.output  # not linked yet
    assert runner.invoke(main, ["link"]).exit_code == 0
    out = runner.invoke(main, ["viz", "linked", "--night", str(NIGHT), "--out-dir", str(tmp_path / "v"), "--no-sheets"])
    assert out.exit_code == 0, out.output
    assert "3 figure(s) written" in out.output and len(list((tmp_path / "v").glob("linked_*.png"))) == 3
