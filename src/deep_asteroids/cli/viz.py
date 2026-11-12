import click

from deep_asteroids.cli import main


@main.group()
@click.pass_context
def viz(ctx):
    """Render cutouts, light curves, coadds and movies for known objects."""


def _identity_slug(tracklet):
    if tracklet.trkid:
        return f"trkid_{tracklet.trkid}"
    if tracklet.orbit_id is not None:
        slug = f"orbit_{tracklet.orbit_id}"
        return f"{slug}_{tracklet.component}" if tracklet.component else slug
    if tracklet.provid:
        return "provid_" + tracklet.provid.replace(" ", "_")
    return f"tracklet_{tracklet.id}"


@viz.command()
@click.option("--source", required=True, type=click.Choice(["synthetic", "mpc_observation", "mpchecker"]))
@click.option("--night", required=True, type=int)
@click.option("--trkid", type=str, help="mpc_observation: the MPC tracklet id")
@click.option("--orbit-id", type=int, help="synthetic: the orbit id")
@click.option("--component", type=click.Choice(["primary", "companion", "barycenter", "photocenter"]),
              help="synthetic: restrict to one binary component (default: every component present)")
@click.option("--provid", type=str, help="mpc_observation/mpchecker: the provisional designation")
@click.option("--permid", type=str, help="mpc_observation/mpchecker: the permanent designation/number")
@click.option("--out-dir", type=click.Path(file_okay=False), default=None,
              help="Default: ./viz/<source>/<night>/<identity>/")
@click.option("--product-type", default="differenceExp", show_default=True)
@click.option("--cutout-size", type=int, default=51, show_default=True)
@click.pass_context
def tracklet(ctx, source, night, trkid, orbit_id, component, provid, permid, out_dir, product_type, cutout_size):
    """Cutouts + forced photometry + light curve + coadd + movie for one
    known_tracklet row (or every component of a synthetic binary, if
    --component is omitted).

    Needs images already ingested (`ingest images`) and the vendored
    cutout_and_photometry engine built (see
    src/deep_asteroids/util/cutout_engine/VENDORED_FROM.md)."""
    from pathlib import Path

    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db, get_deep_db
    from deep_asteroids.util.cutout import LSSTCutoutPhotometry
    from deep_asteroids.viz.tracklet import resolve_tracklets, visualize_tracklet

    if not any((trkid, orbit_id is not None, provid, permid)):
        raise click.UsageError("give at least one of --trkid, --orbit-id, --provid, --permid")

    engine = LSSTCutoutPhotometry()
    deep_engine = get_deep_db()
    with Session(get_db()) as session:
        tracklets = resolve_tracklets(
            session, source, night, trkid=trkid, orbit_id=orbit_id, component=component, provid=provid,
            permid=permid,
        )
        if not tracklets:
            raise click.ClickException("no known_tracklet row matches that identity")

        base_out_dir = Path(out_dir) if out_dir else Path("viz") / source / str(night)
        for t in tracklets:
            # resolve_tracklets() can return more than one row (e.g. every
            # component of a binary) -- each needs its own subdirectory, or
            # they'd silently overwrite each other's plots and even share
            # cutout filenames (deep_db_detector_exposure_id is often the
            # same exposure across a binary's own components).
            if out_dir:
                this_out_dir = base_out_dir / (t.component or str(t.id)) if len(tracklets) > 1 else base_out_dir
            else:
                this_out_dir = base_out_dir / _identity_slug(t)
            summary = visualize_tracklet(session, deep_engine, engine, t, this_out_dir,
                                         product_type=product_type, cutout_size=cutout_size)
            click.echo(summary.format())
