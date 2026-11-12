import click

from deep_asteroids.cli import main

# CLI source name -> known_tracklet.source
TRACKLET_SOURCES = {
    "synthetic": "synthetic",
    "mpc-observations": "mpc_observation",
    "mpc-ephemeris": "mpchecker",
}


@main.group()
@click.pass_context
def ingest(ctx):
    """Ingest search definitions and known-object tracklets from deep-db."""


@ingest.command()
@click.option("--night", "nights", multiple=True, type=int, help="Only these deep-db nights (repeatable)")
@click.option("--force", is_flag=True, help="Replace nights that already exist")
@click.pass_context
def searches(ctx, nights, force):
    """Ingest one search_definition row per (night, field, detector) deep-db knows.

    Band/science-field/minimum-exposure filtering is deferred to the
    search stage; this ingests every triple. Re-running skips nights
    already ingested unless --force."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db, deep_db_sha, get_deep_db
    from deep_asteroids.ingest.searches import ingest_searches

    with Session(create_db()) as session:
        summary = ingest_searches(
            session, get_deep_db(), nights=list(nights) or None, force=force, deep_db_sha=deep_db_sha(),
        )
    click.echo(summary.format())


@ingest.command()
@click.option("--registry-uri", required=True, help="The Butler repo's registry database URL")
@click.option("--repo-dir", required=True, type=click.Path(file_okay=False),
              help="The Butler repo root the registry's paths are relative to")
@click.option("--product-type", "product_types", multiple=True, required=True,
              help="A Butler dataset type to ingest (repeatable), e.g. differenceExp")
@click.option("--night", "nights", multiple=True, type=int, help="Only these deep-db nights (repeatable)")
@click.option("--field", "fields_", multiple=True, type=str, help="Only these deep-db fields (repeatable)")
@click.option("--force", is_flag=True, help="Replace (night, field) groups that already exist")
@click.pass_context
def images(ctx, registry_uri, repo_dir, product_types, nights, fields_, force):
    """Link deep-db detector-exposures to Butler dataset paths, one row per
    (detector-exposure, product type).

    Scoped per (night, field) pointing group -- the real registry has
    thousands of RUN collections from reprocessing, so duplicate
    (visit, detector) rows are only resolved correctly within one
    pointing group's own collection-chain precedence. Re-running skips
    (night, field) groups already ingested unless --force."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db, deep_db_sha, get_deep_db
    from deep_asteroids.ingest.images import ingest_images

    with Session(create_db()) as session:
        summary = ingest_images(
            session, get_deep_db(), registry_uri, repo_dir, product_types,
            nights=list(nights) or None, fields=list(fields_) or None, force=force, deep_db_sha=deep_db_sha(),
        )
    click.echo(summary.format())


@ingest.command()
@click.argument("sources", nargs=-1, type=click.Choice(sorted(TRACKLET_SOURCES)))
@click.option("--night", "nights", multiple=True, type=int, help="Only these deep-db nights (repeatable)")
@click.option("--force", is_flag=True, help="Replace (source, night) rows that already exist")
@click.option(
    "--max-residual-arcsec", type=float, default=None, show_default="5.0",
    help="Flag a fitted tracklet 'large_residual' (fit kept) if a point is further than this from its line",
)
@click.pass_context
def tracklets(ctx, sources, nights, force, max_residual_arcsec):
    """Fit one linear tracklet per known object per night from deep-db.

    SOURCES (default: all): synthetic (synthetic objects, plus binary
    barycenter/photocenter), mpc-observations (MPC observation tracklets),
    mpc-ephemeris (mpchecker predictions). Re-running skips (source, night)
    pairs already ingested unless --force."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db, deep_db_sha, get_deep_db
    from deep_asteroids.ingest.tracklets import MAX_RESIDUAL_ARCSEC, ingest_source

    sources = sources or tuple(TRACKLET_SOURCES)
    deep_engine = get_deep_db()
    sha = deep_db_sha()
    with Session(create_db()) as session:
        for name in sources:
            summary = ingest_source(
                session, deep_engine, TRACKLET_SOURCES[name], nights=list(nights) or None, force=force,
                deep_db_sha=sha,
                max_residual_arcsec=MAX_RESIDUAL_ARCSEC if max_residual_arcsec is None else max_residual_arcsec,
            )
            click.echo(summary.format())
