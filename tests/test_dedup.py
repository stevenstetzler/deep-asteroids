import crossmatch_helpers as h
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.crossmatch.dedup import cluster_results, dedup_results
from deep_asteroids.crossmatch.geometry import positions
from deep_asteroids.database import create_db
from deep_asteroids.database.crossmatch import ResultDedup

V = {"vra": 0.2, "vdec": 0.05}  # a typical main-belt mover, deg/day


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


def _mover(east=0.0, north=0.0, n=10, **kw):
    ra, dec = h.offset(*h.BASE, east, north)
    return {"ra": ra, "dec": dec, **V, "n": n, **kw}


def _rows(session):
    return {r.result_id: r for r in session.scalars(select(ResultDedup))}


def test_clusters_redetections_and_keeps_distinct_objects_apart(session):
    h.add_definition(session, "run")
    # 0 and 1: the same object 0.5" apart; 2: another object 30" away
    r = h.add_run(session, "run", [_mover(n=8), _mover(east=0.5, n=12), _mover(north=30.0)])
    summary = dedup_results(session)
    assert (summary.n_results, summary.n_clusters, summary.n_merged_away) == (3, 2, 1)
    assert summary.cluster_sizes == {2: 1, 1: 1}
    rows = _rows(session)
    assert rows[r[0].id].authoritative_result_id == rows[r[1].id].authoritative_result_id == r[1].id  # most points
    assert (rows[r[0].id].is_authoritative, rows[r[1].id].is_authoritative) == (False, True)
    assert rows[r[2].id].is_authoritative and rows[r[2].id].n_members == 1
    assert rows[r[0].id].n_members == rows[r[1].id].n_members == 2 and rows[r[0].id].merge_radius_arcsec == 2.0
    assert (rows[r[0].id].night, rows[r[0].id].field) == (h.NIGHT, "A0b")


def test_authoritative_ties_go_to_the_lowest_result_id(session):
    h.add_definition(session, "run")
    r = h.add_run(session, "run", [_mover(east=0.4), _mover(), _mover(east=0.8)])
    dedup_results(session)
    assert {x.authoritative_result_id for x in _rows(session).values()} == {r[0].id}


def test_clusters_are_transitive(session):
    """A-B and B-C within the radius, A-C beyond it: one cluster of three."""
    h.add_definition(session, "run")
    h.add_run(session, "run", [_mover(), _mover(east=1.5), _mover(east=3.0)])
    summary = dedup_results(session)
    assert summary.n_clusters == 1 and summary.cluster_sizes == {3: 1}


def test_agreement_is_judged_on_average_over_the_exposures(session):
    """Two tracklets that cross at one exposure but disagree elsewhere are not redetections.

    They are within the radius at the exposure where they cross, so the pair is a candidate
    (``pairs_within`` keeps every pair that is close at any exposure); the mean separation then
    rejects it."""
    h.add_definition(session, "run")
    h.add_run(session, "run", [_mover(), _mover(vra=0.4, vdec=-0.05)])  # cross at T_REF, ~36" apart at the ends
    assert dedup_results(session).n_clusters == 2


def test_merges_across_ccds_of_a_pointing_group_but_not_across_fields_or_nights(session):
    h.add_definition(session, "run_a", detector=1)
    h.add_definition(session, "run_b", detector=2)
    h.add_definition(session, "run_other_field", field="A0c", detector=1)
    h.add_definition(session, "run_other_night", night=20190403, detector=1)
    a = h.add_run(session, "run_a", [_mover(n=5)])
    b = h.add_run(session, "run_b", [_mover(east=0.3, n=9)])
    c = h.add_run(session, "run_other_field", [_mover()])
    d = h.add_run(session, "run_other_night", [_mover()])
    summary = dedup_results(session)
    assert len(summary.groups) == 3 and summary.n_clusters == 3 and summary.n_results == 4
    rows = _rows(session)
    assert rows[a[0].id].authoritative_result_id == rows[b[0].id].authoritative_result_id == b[0].id
    assert rows[c[0].id].is_authoritative and rows[d[0].id].is_authoritative


def test_run_filter_and_idempotence_and_force(session):
    h.add_definition(session, "run_a", detector=1)
    h.add_definition(session, "run_b", detector=2)
    a = h.add_run(session, "run_a", [_mover()])
    h.add_run(session, "run_b", [_mover()])
    only_a = dedup_results(session, run_ids=["run_a"])
    assert only_a.n_results == 1 and only_a.n_clusters == 1 and list(_rows(session)) == [a[0].id]

    again = dedup_results(session, run_ids=["run_a"])
    assert again.skipped_groups == [(h.NIGHT, "A0b")] and not again.groups
    both = dedup_results(session, force=True)
    assert both.n_results == 2 and both.n_clusters == 1 and len(_rows(session)) == 2
    assert dedup_results(session, nights=[20000101]).groups == []


def test_a_group_with_a_new_result_is_redone(session):
    h.add_definition(session, "run_a", detector=1)
    h.add_definition(session, "run_b", detector=2)
    h.add_run(session, "run_a", [_mover()])
    dedup_results(session)
    assert [r.is_authoritative for r in _rows(session).values()] == [True]
    b = h.add_run(session, "run_b", [_mover(east=0.2, n=20)])  # a later search of the same pointing group
    summary = dedup_results(session)
    assert summary.groups == [(h.NIGHT, "A0b")] and summary.n_clusters == 1
    assert {r.authoritative_result_id for r in _rows(session).values()} == {b[0].id}


def test_results_without_a_search_definition_are_counted_not_grouped(session):
    h.add_definition(session, "known_run")
    h.add_run(session, "known_run", [_mover()])
    h.add_run(session, "orphan_run", [_mover(), _mover(north=60)])
    summary = dedup_results(session)
    assert summary.n_results == 1 and summary.n_unassigned == 2
    assert "2 result(s) belong to a run_id with no search definition" in summary.format()


def test_cluster_results_matches_brute_force():
    """The KD-tree prefilter loses no pair a brute-force mean-separation test would merge."""
    import numpy as np

    from deep_asteroids.ingest.fit import sep_arcsec

    rng = np.random.default_rng(0)
    n = 60
    vra, vdec = rng.uniform(0.1, 0.4, n), rng.uniform(-0.2, 0.2, n)
    ra, dec = positions(
        (h.BASE[0] - vra * h.T_REF + rng.uniform(-6, 6, n) * h.ARCSEC) % 360,
        h.BASE[1] - vdec * h.T_REF + rng.uniform(-6, 6, n) * h.ARCSEC, vra, vdec, h.EXPOSURES_TAI)
    labels = cluster_results(ra, dec, 3.0)

    # brute force: union-find over every pair
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if sep_arcsec(ra[i], dec[i], ra[j], dec[j]).mean() < 3.0:
                parent[find(i)] = find(j)
    brute = [find(i) for i in range(n)]
    same_fast = labels[:, None] == labels[None, :]
    same_brute = np.array(brute)[:, None] == np.array(brute)[None, :]
    assert (same_fast == same_brute).all() and 1 < len(set(labels)) < n
