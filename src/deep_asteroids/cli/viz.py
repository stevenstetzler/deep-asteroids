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
@click.option("--annotate", type=str, default=None,
              help="A str.format() template (e.g. '{night}/{field}/{expnum}') drawn in white at the top of "
                   "each movie frame, shrunk/truncated to fit the frame width. Fields: night, field, expnum, "
                   "mjd_tai, ra, dec, mag.")
@click.pass_context
def tracklet(ctx, source, night, trkid, orbit_id, component, provid, permid, out_dir, product_type, cutout_size,
            annotate):
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
                                         product_type=product_type, cutout_size=cutout_size, annotate=annotate)
            click.echo(summary.format())


@viz.command()
@click.option("--night", required=True, type=int)
@click.option("--out-dir", required=True, type=click.Path(file_okay=False), help="Where to write the figures")
@click.option("--min-members", type=int, default=2, show_default=True,
              help="Only linked tracklets built from at least this many MPC tracklets")
@click.option("--linked-id", "linked_ids", multiple=True, type=int,
              help="Only this mpc_linked known_tracklet id (repeatable)")
@click.option("--limit", type=int, default=None, help="Only the first N (largest groups first)")
@click.option("--sheets/--no-sheets", default=True, show_default=True, help="Also tile the figures into contact sheets")
@click.pass_context
def linked(ctx, night, out_dir, min_members, linked_ids, limit, sheets):
    """RA/Dec plots of the linked MPC tracklets, one figure each, for checking `link`.

    Each shows every member trkid's observations and fit separately, the merged
    fit, and how far each member's fit is from the merged one over the night's
    exposures. Needs `ingest tracklets mpc-observations` and `link` done for the night."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db, get_deep_db
    from deep_asteroids.viz.linked import visualize_linked

    with Session(get_db()) as session:
        summary = visualize_linked(session, get_deep_db(), night, out_dir, min_members=min_members,
                                   linked_ids=linked_ids or None, limit=limit, sheets=sheets)
    if not summary.n_groups:
        raise click.ClickException(f"no linked tracklet with at least {min_members} members on night {night} "
                                   "(run `link --night` first?)")
    click.echo(summary.format() + f" in {out_dir}")


@viz.command()
@click.option("--run-id", required=True, type=str, help="A search definition's run_id")
@click.option("--result-num", required=True, type=int, help="The result's index within the run")
@click.option("--out-dir", type=click.Path(file_okay=False), default=None,
              help="Default: ./viz/result/<run_id>/<result_num>/")
@click.option("--product-type", default="differenceExp", show_default=True)
@click.option("--cutout-size", type=int, default=51, show_default=True)
@click.option("--plot-format", type=click.Choice(["png", "pdf"]), default="png", show_default=True,
              help="Format for the light curve/coadd/sky plots (vector with pdf); the movie is always a gif")
@click.option("--annotate", type=str, default=None,
              help="A str.format() template (e.g. '{night}/{field}/{expnum}') drawn in white at the top of "
                   "each movie frame, shrunk/truncated to fit the frame width. Fields: night, field, expnum, "
                   "mjd_tai, ra, dec, mag.")
@click.pass_context
def result(ctx, run_id, result_num, out_dir, product_type, cutout_size, plot_format, annotate):
    """Cutouts + forced photometry + light curve + coadd + movie + sky plot for one
    ingested find-asteroids result, at its own tracklet's predicted position in every
    exposure of its search that the tracklet's fit spans.

    Needs `ingest searches`, `ingest images` and the run's results already in the
    database (`search run`), and the vendored cutout_and_photometry engine built (see
    src/deep_asteroids/util/cutout_engine/VENDORED_FROM.md). Unlike `viz tracklet`,
    cutouts are discarded once rendered -- only the four plots are kept."""
    from pathlib import Path

    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.util.cutout import LSSTCutoutPhotometry
    from deep_asteroids.viz.result import visualize_result

    engine = LSSTCutoutPhotometry()
    out_dir = Path(out_dir) if out_dir else Path("viz") / "result" / run_id / str(result_num)
    with Session(get_db()) as session:
        summary = visualize_result(session, engine, run_id, result_num, out_dir, product_type=product_type,
                                   cutout_size=cutout_size, annotate=annotate, plot_format=plot_format)
    if summary is None:
        raise click.ClickException(f"no result {result_num} for run_id {run_id} (run `ingest results` first?)")
    click.echo(summary.format())
