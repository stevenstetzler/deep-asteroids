import click

from deep_asteroids.cli import main

DEFAULT_TRACKLET_FIELDS = (
    "source", "night", "component", "orbit_id", "trkid", "provid",
    "epoch", "ra_epoch", "dec_epoch", "vra", "vdec", "n_points", "max_residual_arcsec", "flag",
)


@main.group()
@click.pass_context
def query(ctx):
    """Query the deep-asteroids database."""


@query.command()
@click.argument("fields", nargs=-1, type=str)
@click.option("--source", "sources", multiple=True,
              type=click.Choice(["synthetic", "mpc_observation", "mpchecker"]))
@click.option("--night", "nights", multiple=True, type=int)
@click.option("--provid", "provids", multiple=True, type=str)
@click.option("--trkid", "trkids", multiple=True, type=str)
@click.option("--orbit-id", "orbit_ids", multiple=True, type=int)
@click.option("--component", "components", multiple=True,
              type=click.Choice(["primary", "companion", "barycenter", "photocenter"]))
@click.option("--flag", "flags", multiple=True, type=str,
              help="Only rows with this flag (repeatable); 'none' selects unflagged rows")
@click.option("--header/--no-header", default=False, help="Print the field names first")
@click.option("--output", type=click.Path(dir_okay=False), help="Write here instead of stdout")
@click.pass_context
def tracklets(ctx, fields, sources, nights, provids, trkids, orbit_ids, components, flags, header, output):
    """Print known_tracklet rows, '|'-separated (deep-db's query style).

    FIELDS are known_tracklet column names (default: a summary set)."""
    from sqlalchemy import or_, select
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.database.tracklets import KnownTracklet

    fields = fields or DEFAULT_TRACKLET_FIELDS
    columns = KnownTracklet.__table__.columns
    unknown = [f for f in fields if f not in columns]
    if unknown:
        raise click.BadParameter(f"unknown field(s) {unknown}; choose from {list(columns.keys())}",
                                 param_hint="FIELDS")

    q = select(*(columns[f] for f in fields))
    for column, values in (("source", sources), ("night", nights), ("provid", provids), ("trkid", trkids),
                           ("orbit_id", orbit_ids), ("component", components)):
        if values:
            q = q.where(columns[column].in_(values))
    if flags:
        named = [f for f in flags if f != "none"]
        conditions = ([columns["flag"].in_(named)] if named else []) + (
            [columns["flag"].is_(None)] if "none" in flags else [])
        q = q.where(or_(*conditions))
    q = q.order_by(columns["id"])

    with click.open_file(output or "-", "w") as out, Session(get_db()) as session:
        if header:
            out.write("|".join(fields) + "\n")
        for row in session.execute(q):
            out.write("|".join("" if v is None else str(v) for v in row) + "\n")
