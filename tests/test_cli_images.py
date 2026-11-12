from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, text

from deep_asteroids.cli import main

NIGHT = 20190402
FIELD = "A0b"
REPO_DIR = "/repo"
EXPNUMS = (845580, 845581, 845582)  # matches conftest's _seed_deep_db
DETECTORS = (4, 5)


@pytest.fixture
def runner(db_url, deep_db_url):
    return CliRunner()


def _make_registry(tmp_path: Path) -> str:
    """A minimal Butler-registry stand-in, scoped under one RUN chained
    into DEEP/<night>/<field> -- the shape ingest/images.py expects. See
    test_ingest_images.py's own (more thorough) version of this helper."""
    db_path = tmp_path / "registry.sqlite"
    uri = f"sqlite:///{db_path}"
    engine = create_engine(uri)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE dataset_type (id INTEGER PRIMARY KEY, name TEXT, tag_association_table TEXT)"))
        conn.execute(text(
            "CREATE TABLE dataset_tags_1 (dataset_type_id INTEGER, dataset_id TEXT, visit INTEGER, detector INTEGER)"
        ))
        conn.execute(text("CREATE TABLE file_datastore_records (dataset_id TEXT, path TEXT)"))
        conn.execute(text("CREATE TABLE dataset (id TEXT, run_id INTEGER)"))
        conn.execute(text("CREATE TABLE run (collection_id INTEGER)"))
        conn.execute(text("CREATE TABLE collection (collection_id INTEGER, name TEXT, type INTEGER)"))
        conn.execute(text("CREATE TABLE collection_chain (parent INTEGER, position INTEGER, child INTEGER)"))

        conn.execute(
            text("INSERT INTO dataset_type (id, name, tag_association_table) VALUES (1, 'differenceExp', 'dataset_tags_1')")
        )
        conn.execute(text("INSERT INTO collection (collection_id, name, type) VALUES (1, :name, 3)"),
                    {"name": f"DEEP/{NIGHT}/{FIELD}"})
        conn.execute(text("INSERT INTO collection (collection_id, name, type) VALUES (10, 'run-a', 1)"))
        conn.execute(text("INSERT INTO collection_chain (parent, position, child) VALUES (1, 0, 10)"))
        conn.execute(text("INSERT INTO run (collection_id) VALUES (10)"))
        for expnum in EXPNUMS:
            for detector in DETECTORS:
                dsid = f"ds-{expnum}-{detector}"
                conn.execute(
                    text("INSERT INTO dataset_tags_1 (dataset_type_id, dataset_id, visit, detector) "
                         "VALUES (1, :dsid, :visit, :detector)"),
                    {"dsid": dsid, "visit": expnum, "detector": detector},
                )
                conn.execute(text("INSERT INTO file_datastore_records (dataset_id, path) VALUES (:dsid, :path)"),
                            {"dsid": dsid, "path": f"im/{expnum}_{detector}.fits"})
                conn.execute(text("INSERT INTO dataset (id, run_id) VALUES (:dsid, 10)"), {"dsid": dsid})
    engine.dispose()
    return uri


def test_ingest_and_query_images(runner, tmp_path):
    registry_uri = _make_registry(tmp_path)
    result = runner.invoke(main, [
        "ingest", "images", "--registry-uri", registry_uri, "--repo-dir", REPO_DIR,
        "--product-type", "differenceExp",
    ])
    assert result.exit_code == 0, result.output
    assert "1 (night, field) group(s) ingested" in result.output
    assert "6 image(s) written" in result.output

    again = runner.invoke(main, [
        "ingest", "images", "--registry-uri", registry_uri, "--repo-dir", REPO_DIR,
        "--product-type", "differenceExp",
    ])
    assert "0 (night, field) group(s) ingested, 1 skipped" in again.output

    rows_result = runner.invoke(main, ["query", "images", "expnum", "detector_number", "uri",
                                       "--detector-number", "4"])
    assert rows_result.exit_code == 0, rows_result.output
    rows = [line.split("|") for line in rows_result.output.splitlines()]
    assert sorted(int(r[0]) for r in rows) == list(EXPNUMS)
    assert all(r[1] == "4" for r in rows)
    assert all(r[2] == f"{REPO_DIR}/im/{r[0]}_4.fits" for r in rows)


def test_query_images_unknown_field(runner):
    result = runner.invoke(main, ["query", "images", "nope"])
    assert result.exit_code != 0 and "unknown field" in result.output
