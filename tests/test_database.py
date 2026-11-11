from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from deep_asteroids import database
from deep_asteroids.util.deep_db import cached_sha256, sqlite_readonly_url

FIND_ASTEROIDS_TABLES = {"search", "result", "tracklet", "gathered", "points"}


def test_db_url_defaults_to_sqlite_file_in_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEP_ASTEROIDS_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert database.get_db_url() == f"sqlite:///{tmp_path / 'deep_asteroids.sqlite'}"


def test_db_url_env_override(db_url):
    assert database.get_db_url() == db_url


def test_create_db_creates_find_asteroids_tables(db_url):
    engine = database.create_db()
    assert FIND_ASTEROIDS_TABLES <= set(inspect(engine).get_table_names())


def test_create_db_force_recreates(db_url):
    engine = database.create_db()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO search (run_id) VALUES ('x')"))
    engine = database.create_db(force=True)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM search")).scalar() == 0


def test_sqlite_readonly_url_is_read_only_and_immutable(deep_db_path):
    url = sqlite_readonly_url(deep_db_path)
    assert url.startswith("sqlite:///file:/")
    assert "mode=ro" in url and "immutable=1" in url and "uri=true" in url


def test_deep_db_is_not_writable(deep_db_url):
    with pytest.raises(Exception, match="readonly|read-only"), database.get_deep_db().begin() as conn:
        conn.execute(text("DELETE FROM night"))


def test_deep_db_url_env_override(deep_db_url):
    assert database.get_deep_db_url() == deep_db_url
    with database.get_deep_db().connect() as conn:
        assert conn.execute(text("SELECT night FROM night")).scalar() == 20190402


@pytest.mark.parametrize("form", ["plain_path", "four_slash_url", "uri_form_url"])
def test_deep_db_env_forms_open_read_only(deep_db_path, monkeypatch, form):
    value = {
        "plain_path": str(deep_db_path),
        "four_slash_url": f"sqlite:///{deep_db_path}",  # deep_db_path is absolute -> sqlite:////...
        "uri_form_url": f"sqlite:///file:{deep_db_path}?mode=ro&uri=true",
    }[form]
    monkeypatch.setenv("DEEP_DB", value)
    assert database.get_deep_db_url() == sqlite_readonly_url(deep_db_path)
    with database.get_deep_db().connect() as conn:
        assert conn.execute(text("SELECT night FROM night")).scalar() == 20190402
    database.dispose_engines()


def test_deep_db_three_slash_url_is_relative_and_reported(deep_db_path, tmp_path, monkeypatch):
    # The reported bug: `sqlite:///abs/path` (three slashes) is the *relative*
    # path `abs/path`, resolved against cwd -- it must fail clearly, naming the
    # path it actually looked at, not with sqlite's "unable to open database file".
    monkeypatch.chdir(tmp_path)
    value = "sqlite:///" + str(deep_db_path).lstrip("/")
    monkeypatch.setenv("DEEP_DB", value)
    with pytest.raises(FileNotFoundError) as excinfo:
        database.get_deep_db_url()
    message = str(excinfo.value)
    assert str(tmp_path / str(deep_db_path).lstrip("/")) in message
    assert "sqlite:////" in message


def test_deep_db_missing_file_is_not_created(tmp_path, monkeypatch):
    missing = tmp_path / "nope" / "deep_database.sqlite"
    missing.parent.mkdir()
    monkeypatch.setenv("DEEP_DB", f"sqlite:///{missing}")
    with pytest.raises(FileNotFoundError, match=str(missing)):
        database.get_deep_db()
    assert not missing.exists()  # sqlite would otherwise silently create an empty database here


@pytest.mark.parametrize("url", ["postgresql://host:5432/deep_db", "sqlite://"])
def test_deep_db_non_file_urls_pass_through(url, monkeypatch):
    monkeypatch.setenv("DEEP_DB", url)
    assert database.get_deep_db_url() == url


def test_deep_db_url_falls_back_to_deep_db_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEP_DB", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cached = tmp_path / "deep_db" / "deep_database.sqlite"
    cached.parent.mkdir()
    cached.touch()
    assert database.deep_db_cache_path() == cached
    assert database.get_deep_db_url() == sqlite_readonly_url(cached)


def test_missing_deep_db_raises_and_never_downloads(tmp_path, monkeypatch):
    import deep_db.database

    monkeypatch.delenv("DEEP_DB", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    def no_download():
        raise AssertionError("get_deep_db_url() must never download deep-db")

    monkeypatch.setattr(deep_db.database, "download_db", no_download)
    with pytest.raises(FileNotFoundError, match="fetch-deep-db"):
        database.get_deep_db_url()


def test_cached_sha256_reads_sidecar_only(tmp_path):
    db = tmp_path / "deep_database.sqlite"
    db.write_bytes(b"not really sqlite")
    assert cached_sha256(db) is None
    Path(str(db) + ".sha256").write_text("abc123\n")
    assert cached_sha256(db) == "abc123"
