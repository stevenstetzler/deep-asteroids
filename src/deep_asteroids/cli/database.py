import click

from deep_asteroids.cli import main

# deep-db tables `database info` reports row counts for
DEEP_DB_INFO_TABLES = (
    "night",
    "exposure",
    "detector_exposure",
    "mpc_observation",
    "mpchecker_ephemeris",
    "synthetic_object_ephemeris",
)


@main.group()
@click.pass_context
def database(ctx):
    """Create and inspect the deep-asteroids database; fetch deep-db."""


@database.command()
@click.option("--force", is_flag=True, help="Delete and recreate the database first (drop-and-re-ingest)")
@click.pass_context
def create(ctx, force):
    """Create the deep-asteroids database tables."""
    from deep_asteroids.database import create_db, get_db_url

    create_db(force=force)
    click.echo(f"created {get_db_url()}")


@database.command()
@click.option(
    "--exact", is_flag=True,
    help="Exact deep-db row counts via count(*) (scans each table: ~1.5 min cold on GPFS). "
         "By default deep-db's SQLite tables report max(rowid), instant and equal to the row "
         "count for an append-only snapshot.",
)
@click.pass_context
def info(ctx, exact):
    """Show both database URLs, our table row counts, and the deep-db file's identity and row counts."""
    from sqlalchemy import inspect, text
    from sqlalchemy.engine import make_url

    from deep_asteroids.database import get_db, get_db_url, get_deep_db, get_deep_db_url
    from deep_asteroids.util.deep_db import cached_sha256, sqlite_path_from_url

    click.echo(f"deep-asteroids: {get_db_url()}")
    engine = get_db()
    tables = sorted(inspect(engine).get_table_names())
    if not tables:
        click.echo("  (no tables -- run `deep-asteroids database create`)")
    with engine.connect() as conn:
        for table in tables:
            n = conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
            click.echo(f"  {table:32s}{n:>14,}")

    try:
        deep_db_url = get_deep_db_url()
    except FileNotFoundError as e:
        click.echo(f"deep-db: {e}")
        return
    click.echo(f"deep-db: {deep_db_url}")
    is_sqlite = make_url(deep_db_url).get_backend_name() == "sqlite"
    path = sqlite_path_from_url(deep_db_url)
    if path is not None and path.exists():
        click.echo(f"  path   {path}")
        click.echo(f"  size   {path.stat().st_size:,} bytes")
        click.echo(f"  sha256 {cached_sha256(path) or 'unknown (no .sha256 sidecar; see fetch-deep-db)'}")
    count = "count(*)" if exact or not is_sqlite else "coalesce(max(rowid), 0)"
    with get_deep_db().connect() as conn:
        for table in DEEP_DB_INFO_TABLES:
            n = conn.execute(text(f'SELECT {count} FROM "{table}"')).scalar()
            click.echo(f"  {table:32s}{n:>14,}")


@database.command(name="fetch-deep-db")
@click.pass_context
def fetch_deep_db(ctx):
    """Download (or confirm up to date) the published deep-db SQLite file.

    Run this once, explicitly, on a login node: it is the only command that
    downloads anything. Everything else opens the cached copy read-only."""
    from deep_db.database import download_db

    path = download_db()
    click.echo(path)
