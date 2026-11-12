from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from deep_asteroids.database import create_db, get_deep_db
from deep_asteroids.database.images import Image
from deep_asteroids.ingest.images import ingest_images

NIGHT = 20190402
FIELD = "A0b"
REPO_DIR = "/repo"
EXPNUMS = (845580, 845581, 845582)  # matches conftest's _seed_deep_db
DETECTORS = (4, 5)


@pytest.fixture
def session(db_url, deep_db_url):
    with Session(create_db()) as session:
        yield session


def _make_registry(tmp_path: Path, rows, collections=(), chain_edges=(), product_type="differenceExp",
                    table_name="dataset_tags_00000042"):
    """A minimal stand-in for a Butler registry (the tables/columns
    ingest/images.py actually touches), in an SQLite file. ``rows``:
    (visit, detector, dataset_id, path, run_collection_id). ``collections``:
    (collection_id, name, type) -- RUN=1, CHAINED=3. ``chain_edges``:
    (parent, position, child)."""
    db_path = tmp_path / "registry.sqlite"
    uri = f"sqlite:///{db_path}"
    engine = create_engine(uri)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE dataset_type (id INTEGER PRIMARY KEY, name TEXT, tag_association_table TEXT)"))
        conn.execute(text(
            f"CREATE TABLE {table_name} (dataset_type_id INTEGER, dataset_id TEXT, visit INTEGER, detector INTEGER)"
        ))
        conn.execute(text("CREATE TABLE file_datastore_records (dataset_id TEXT, path TEXT)"))
        conn.execute(text("CREATE TABLE dataset (id TEXT, run_id INTEGER)"))
        conn.execute(text("CREATE TABLE run (collection_id INTEGER)"))
        conn.execute(text("CREATE TABLE collection (collection_id INTEGER, name TEXT, type INTEGER)"))
        conn.execute(text("CREATE TABLE collection_chain (parent INTEGER, position INTEGER, child INTEGER)"))

        conn.execute(
            text("INSERT INTO dataset_type (id, name, tag_association_table) VALUES (1, :name, :table)"),
            {"name": product_type, "table": table_name},
        )
        seen_runs = set()
        for visit, detector, dataset_id, path, run_collection_id in rows:
            conn.execute(
                text(f"INSERT INTO {table_name} (dataset_type_id, dataset_id, visit, detector) "
                     "VALUES (1, :dsid, :visit, :detector)"),
                {"dsid": dataset_id, "visit": visit, "detector": detector},
            )
            conn.execute(
                text("INSERT INTO file_datastore_records (dataset_id, path) VALUES (:dsid, :path)"),
                {"dsid": dataset_id, "path": path},
            )
            conn.execute(text("INSERT INTO dataset (id, run_id) VALUES (:dsid, :run_id)"),
                        {"dsid": dataset_id, "run_id": run_collection_id})
            if run_collection_id not in seen_runs:
                seen_runs.add(run_collection_id)
                conn.execute(text("INSERT INTO run (collection_id) VALUES (:run_id)"), {"run_id": run_collection_id})
        for collection_id, name, type_ in collections:
            conn.execute(text("INSERT INTO collection (collection_id, name, type) VALUES (:id, :name, :type)"),
                        {"id": collection_id, "name": name, "type": type_})
        for parent, position, child in chain_edges:
            conn.execute(text("INSERT INTO collection_chain (parent, position, child) VALUES (:p, :pos, :c)"),
                        {"p": parent, "pos": position, "c": child})
    engine.dispose()
    return uri


def _scoped_rows(run_collection_id=10):
    """One dataset per (expnum, detector), under one RUN, scoped to the
    fixture deep-db's own DEEP/20190402/A0b collection."""
    rows = []
    for expnum in EXPNUMS:
        for detector in DETECTORS:
            rows.append((expnum, detector, f"ds-{expnum}-{detector}", f"im/{expnum}_{detector}.fits",
                        run_collection_id))
    return rows


def _images(session):
    return session.scalars(select(Image).order_by(Image.id)).all()


def test_ingest_images_basic(session, tmp_path):
    registry_uri = _make_registry(
        tmp_path, rows=_scoped_rows(),
        collections=[(1, f"DEEP/{NIGHT}/{FIELD}", 3), (10, "run-a", 1)],
        chain_edges=[(1, 0, 10)],
    )
    summary = ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["differenceExp"], deep_db_sha="abc")
    assert summary.groups == [(NIGHT, FIELD)]
    assert summary.n_written == len(EXPNUMS) * len(DETECTORS)
    assert summary.n_duplicates_resolved == 0
    assert summary.n_unmatched == 0

    rows = _images(session)
    assert len(rows) == 6
    by_key = {(r.expnum, r.detector_number): r for r in rows}
    for expnum in EXPNUMS:
        for detector in DETECTORS:
            row = by_key[(expnum, detector)]
            assert row.night == NIGHT and row.field == FIELD
            assert row.product_type == "differenceExp"
            assert row.uri == f"{REPO_DIR}/im/{expnum}_{detector}.fits"
            assert row.deep_db_detector_exposure_id is not None
            assert row.deep_db_exposure_id is not None
            assert row.deep_db_sha == "abc"


def test_ingest_images_resolves_duplicate_via_chain(session, tmp_path):
    """Two dataset rows for the same (visit, detector) in two different
    RUNs, chained at different positions -- the row from the RUN ranked
    first (position=0) should win."""
    rows = _scoped_rows()
    # a second, lower-priority copy of one (visit, detector) in another RUN
    rows.append((EXPNUMS[0], DETECTORS[0], "ds-old", "im/old.fits", 20))

    registry_uri = _make_registry(
        tmp_path, rows=rows,
        collections=[(1, f"DEEP/{NIGHT}/{FIELD}", 3), (10, "run-new", 1), (20, "run-old", 1)],
        chain_edges=[(1, 0, 10), (1, 1, 20)],
    )
    summary = ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["differenceExp"])
    assert summary.n_duplicates_resolved == 1

    rows = {(r.expnum, r.detector_number): r for r in _images(session)}
    assert rows[(EXPNUMS[0], DETECTORS[0])].uri == f"{REPO_DIR}/im/{EXPNUMS[0]}_{DETECTORS[0]}.fits"


def test_ingest_images_deterministic_fallback_without_collection(session, tmp_path):
    """No CHAINED collection named DEEP/<night>/<field> in the registry --
    falls back to lowest dataset.id, deterministically."""
    registry_uri = _make_registry(tmp_path, rows=_scoped_rows(), collections=[], chain_edges=[])
    summary = ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["differenceExp"])
    assert summary.n_written == len(EXPNUMS) * len(DETECTORS)


def test_ingest_images_skips_unmatched_registry_rows(session, tmp_path):
    """A registry row for an (expnum, detector) deep-db doesn't know --
    here, a detector deep-db has no geometry for on this expnum (e.g. a
    CCD the ingest never saw) -- skipped, not written, counted. (The
    expnum itself must be one of the group's own, or the `visits` SQL
    filter excludes it before this check ever runs.)"""
    rows = _scoped_rows() + [(EXPNUMS[0], 99, "ds-extra", "im/extra.fits", 10)]
    registry_uri = _make_registry(
        tmp_path, rows=rows,
        collections=[(1, f"DEEP/{NIGHT}/{FIELD}", 3), (10, "run-a", 1)], chain_edges=[(1, 0, 10)],
    )
    summary = ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["differenceExp"])
    assert summary.n_written == len(EXPNUMS) * len(DETECTORS)
    assert summary.n_unmatched == 1


def test_ingest_images_unknown_product_type_is_skipped(session, tmp_path):
    registry_uri = _make_registry(
        tmp_path, rows=_scoped_rows(), collections=[(1, f"DEEP/{NIGHT}/{FIELD}", 3), (10, "run-a", 1)],
        chain_edges=[(1, 0, 10)],
    )
    summary = ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["not-a-real-product"])
    assert summary.n_written == 0


def test_ingest_images_rerun_skips_and_force(session, tmp_path):
    registry_uri = _make_registry(
        tmp_path, rows=_scoped_rows(), collections=[(1, f"DEEP/{NIGHT}/{FIELD}", 3), (10, "run-a", 1)],
        chain_edges=[(1, 0, 10)],
    )
    ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["differenceExp"], deep_db_sha="old")
    again = ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["differenceExp"])
    assert again.groups == []
    assert again.skipped_groups == [(NIGHT, FIELD)]
    assert len(_images(session)) == 6  # not duplicated

    forced = ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["differenceExp"],
                           force=True, deep_db_sha="new")
    assert forced.groups == [(NIGHT, FIELD)]
    rows = _images(session)
    assert len(rows) == 6
    assert all(r.deep_db_sha == "new" for r in rows)


def test_ingest_images_night_field_filter(session, tmp_path):
    registry_uri = _make_registry(
        tmp_path, rows=_scoped_rows(), collections=[(1, f"DEEP/{NIGHT}/{FIELD}", 3), (10, "run-a", 1)],
        chain_edges=[(1, 0, 10)],
    )
    summary = ingest_images(session, get_deep_db(), registry_uri, REPO_DIR, ["differenceExp"],
                            nights=[20200101])
    assert summary.groups == []
    assert _images(session) == []
