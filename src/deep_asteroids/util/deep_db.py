"""Helpers for reading the published deep-db SQLite file."""
from pathlib import Path


def sqlite_readonly_url(path):
    """A SQLAlchemy URL opening the SQLite file at ``path`` read-only and
    immutable. ``immutable=1`` tells SQLite the file never changes, so it
    skips locking entirely -- safe for a published snapshot nobody writes
    to, and avoids lock traffic on a shared parallel filesystem when many
    processes read it at once."""
    path = Path(path).resolve()
    return f"sqlite:///file:{path}?mode=ro&immutable=1&uri=true"


def sqlite_path_from_url(url):
    """The filesystem path a SQLite SQLAlchemy URL refers to, exactly as
    SQLite would open it (relative paths stay relative), or ``None`` if
    ``url`` isn't a SQLite file URL (another backend, or ``:memory:``).
    Handles both ``sqlite:///path`` and the URI form
    ``sqlite:///file:path?mode=ro&...&uri=true``."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        return None
    database = parsed.database
    if database.startswith("file:"):
        database = database.removeprefix("file:").split("?", 1)[0]
    return Path(database)


def cached_sha256(path):
    """The sha256 deep-db's ``download_db()`` recorded for ``path`` in its
    ``<file>.sha256`` sidecar, or ``None`` if there is no sidecar. Never
    hashes the file itself (1.7 GB)."""
    sidecar = Path(str(path) + ".sha256")
    if not sidecar.exists():
        return None
    return sidecar.read_text().strip().split()[0]
