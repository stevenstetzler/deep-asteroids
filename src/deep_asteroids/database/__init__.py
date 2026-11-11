"""
The two databases deep-asteroids talks to.

* **deep-asteroids** (ours, read/write): search definitions, known-object
  tracklets, and find-asteroids results. Defaults to a SQLite file named
  ``deep_asteroids.sqlite`` in the current directory; ``$DEEP_ASTEROIDS_DB``
  overrides it with any SQLAlchemy URL. Shares ``find_asteroids.models.Base``
  metadata (see ``models.py``), so find-asteroids' own tables live in the same
  database and join against ours in plain SQL.
* **deep-db** (read-only): survey geometry, MPC observations, mpchecker
  ephemerides, synthetic objects. ``$DEEP_DB`` (a SQLAlchemy URL) if set,
  otherwise deep-db's own cached copy of the published SQLite file, opened
  read-only and immutable. This module never downloads it: run
  ``deep-asteroids database fetch-deep-db`` once (on a login node) instead,
  so worker processes never race to download or re-checksum a 1.7 GB file.

Schema policy for now is drop-and-re-ingest (``deep-asteroids database
create --force``) -- there are no migrations.
"""
import os
from pathlib import Path

DB_NAME = "deep_asteroids.sqlite"
DB_ENV = "DEEP_ASTEROIDS_DB"
DEEP_DB_ENV = "DEEP_DB"

_engines = {}


def get_db_url():
    """The deep-asteroids database URL: ``$DEEP_ASTEROIDS_DB``, else ``sqlite:///<cwd>/deep_asteroids.sqlite``."""
    url = os.environ.get(DB_ENV)
    if url:
        return url
    return f"sqlite:///{Path.cwd() / DB_NAME}"


def get_db(echo=False):
    """A (cached, per-URL) engine for the deep-asteroids database."""
    from sqlalchemy import create_engine

    url = get_db_url()
    key = (url, echo)
    if key not in _engines:
        if echo:
            from deep_db.database import ensure_echo_to_stderr

            ensure_echo_to_stderr()
        _engines[key] = create_engine(url, echo=echo)
    return _engines[key]


def create_db(force=False):
    """Create every table on the deep-asteroids database. With ``force``,
    delete a SQLite file first (or drop all tables for any other backend)
    -- the drop-and-re-ingest policy."""
    from sqlalchemy.engine import make_url

    from deep_asteroids.database.models import Base

    import_models()
    url = make_url(get_db_url())
    if force:
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            dispose_engines()
            Path(url.database).unlink(missing_ok=True)
        else:
            Base.metadata.drop_all(get_db())
    engine = get_db()
    Base.metadata.create_all(engine)
    return engine


def import_models():
    """Import every module that declares deep-asteroids tables, registering
    them on ``Base.metadata`` (find-asteroids' tables come with ``models``)."""
    import deep_asteroids.database.models
    import deep_asteroids.database.tracklets  # noqa: F401


def dispose_engines():
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()


def deep_db_cache_path():
    """Where deep-db keeps its downloaded SQLite file (``$XDG_CACHE_HOME/deep_db/``)."""
    from deep_db.database import DB_NAME as DEEP_DB_NAME

    xdg_cache_home = os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(xdg_cache_home) / "deep_db" / DEEP_DB_NAME


def get_deep_db_url():
    """The deep-db URL: ``$DEEP_DB`` if set, else deep-db's cached SQLite
    file. Never downloads.

    ``$DEEP_DB`` may be a plain filesystem path or any SQLAlchemy URL. Any
    SQLite target (path or ``sqlite://`` URL) must already exist, and is
    always opened read-only and immutable. This matters because SQLite
    otherwise silently creates an empty database for a mistyped path --
    e.g. ``sqlite:///abs/path`` (three slashes) is the *relative* path
    ``abs/path``; an absolute path needs four. Non-SQLite URLs pass through
    unchanged. Raises ``FileNotFoundError`` naming the resolved path if a
    SQLite file is missing."""
    from deep_asteroids.util.deep_db import sqlite_path_from_url, sqlite_readonly_url

    value = os.environ.get(DEEP_DB_ENV)
    if value:
        if "://" not in value:
            path = Path(value).expanduser()
            hint = ""
        else:
            path = sqlite_path_from_url(value)
            if path is None:
                return value  # not SQLite (e.g. postgresql://): use as given
            hint = (
                "; note sqlite:///<path> is relative to the current directory -- use "
                "sqlite:////<path> (four slashes) for an absolute path, or set it to a plain file path"
                if not path.is_absolute() else ""
            )
        resolved = path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(
                f"${DEEP_DB_ENV}={value!r} points at {resolved}, which does not exist{hint}"
            )
        return sqlite_readonly_url(resolved)

    path = deep_db_cache_path()
    if not path.exists():
        raise FileNotFoundError(
            f"deep-db database not found at {path}; run `deep-asteroids database fetch-deep-db` "
            f"(on a login node), or set ${DEEP_DB_ENV}"
        )
    return sqlite_readonly_url(path)


def get_deep_db(echo=False):
    """A (cached, per-URL) engine for deep-db. Read-only by construction for
    the cached SQLite file; never creates tables (unlike ``deep_db.database.get_db()``)."""
    from sqlalchemy import create_engine

    url = get_deep_db_url()
    key = (url, echo)
    if key not in _engines:
        if echo:
            from deep_db.database import ensure_echo_to_stderr

            ensure_echo_to_stderr()
        _engines[key] = create_engine(url, echo=echo)
    return _engines[key]
