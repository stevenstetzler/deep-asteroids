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


def cached_sha256(path):
    """The sha256 deep-db's ``download_db()`` recorded for ``path`` in its
    ``<file>.sha256`` sidecar, or ``None`` if there is no sidecar. Never
    hashes the file itself (1.7 GB)."""
    sidecar = Path(str(path) + ".sha256")
    if not sidecar.exists():
        return None
    return sidecar.read_text().strip().split()[0]
