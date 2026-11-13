import click

from deep_asteroids.cli import main


@main.group()
@click.pass_context
def search(ctx):
    """Detect sources and run find-asteroids searches over search definitions."""


def _band(value):
    """``--band``: omitted -> any band; ``''``/``none`` -> the undefined-band rows."""
    if value is None:
        return Ellipsis
    return None if value.lower() in ("", "none") else value


def _selection_options(f):
    for option in reversed([
        click.option("--run-id", "run_ids", multiple=True, help="A search definition's run_id (repeatable)"),
        click.option("--night", type=int), click.option("--field", "field_", type=str),
        click.option("--detector-number", type=int),
        click.option("--band", type=str, help="A band, or '' / none for the undefined-band (all-band) rows"),
        click.option("--catalog-dir", type=click.Path(file_okay=False), default=None,
                     show_default="/gscratch/scrubbed/$USER/deep_asteroids/catalogs",
                     help="Where the per-image detection catalogs live"),
        click.option("--threshold", type=float, default=5.0, show_default=True, help="Detection threshold (sigma)"),
        click.option("--polarity", type=click.Choice(["positive", "negative", "both"]), default="positive",
                     show_default=True),
        click.option("--workers", type=int, default=None, show_default="the Slurm allocation",
                     help="Images detected at once"),
    ]):
        f = option(f)
    return f


def _resolve(session, run_ids, night, field_, detector_number, band):
    from deep_asteroids.search.run import resolve_search_definitions

    if not (run_ids or night is not None or field_ or detector_number is not None):
        raise click.UsageError("select at least one search definition (--run-id, or --night/--field/--detector-number)")
    definitions = resolve_search_definitions(
        session, run_ids=run_ids, night=night, field=field_, detector_number=detector_number, band=_band(band))
    if not definitions:
        raise click.ClickException("no search definition matches (run `ingest searches` first?)")
    return definitions


@search.command()
@_selection_options
@click.option("--force", is_flag=True, help="Redetect catalogs that already exist")
@click.pass_context
def detect(ctx, run_ids, night, field_, detector_number, band, catalog_dir, threshold, polarity, workers, force):
    """Write the per-image detection catalogs for the selected search definitions."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db
    from deep_asteroids.search.detect import default_catalog_dir, detect_exposures
    from deep_asteroids.util.detect import LSSTDetector

    catalog_dir = catalog_dir or default_catalog_dir()
    detector = LSSTDetector(threshold=threshold, polarity=polarity)
    with Session(create_db()) as session:
        for sd in _resolve(session, run_ids, night, field_, detector_number, band):
            s = detect_exposures(session, sd, catalog_dir, detector, workers=workers, force=force)
            click.echo(f"{sd.run_id}: {s.n_written} catalog(s) written ({s.n_detections:,} detections), "
                       f"{s.n_skipped} already present, in {catalog_dir}")


@search.command()
@_selection_options
@click.option("--velocity", nargs=2, type=float, default=(0.1, 0.5), show_default=True, help="Min Max, deg/day")
@click.option("--angle", nargs=2, type=float, default=(0.0, 359.99), show_default=True, help="Min Max, deg")
@click.option("--dx", type=float, default=10.0, show_default=True)
@click.option("--num-results", type=int, default=50, show_default=True)
@click.option("--refine-iterations", type=int, default=1, show_default=True)
@click.option("--seed", type=int, default=0, show_default=True,
              help="Seeds numpy's RNG around the refinement (restored afterwards), so reruns repeat")
@click.option("--shutter-offset-seconds", type=float, default=0.0, show_default=True,
              help="Added to deep-db's exposure midpoint to label each exposure. 0 once deep-db's midpoint "
                   "includes the DECam shutter latency; 0.5 reproduces salad's earlier run")
@click.option("--force", is_flag=True, help="Replace runs that already exist")
@click.option("--remove-catalogs", is_flag=True,
              help="Delete this search's own catalog files once its results are ingested")
@click.pass_context
def run(ctx, run_ids, night, field_, detector_number, band, catalog_dir, threshold, polarity, workers, velocity,
        angle, dx, num_results, refine_iterations, seed, shutter_offset_seconds, force, remove_catalogs):
    """Detect (any missing catalogs), search and ingest the selected search definitions.

    Each run lands under its search definition's run_id, with a search_run
    row recording the seed, the time offset and what each stage cost.
    Re-running skips run_ids already searched unless --force."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db
    from deep_asteroids.search.detect import default_catalog_dir
    from deep_asteroids.search.run import run_search_for_definition
    from deep_asteroids.util.detect import LSSTDetector

    catalog_dir = catalog_dir or default_catalog_dir()
    detector = LSSTDetector(threshold=threshold, polarity=polarity)
    with Session(create_db()) as session:
        for sd in _resolve(session, run_ids, night, field_, detector_number, band):
            summary = run_search_for_definition(
                session, sd, catalog_dir, detector, velocity=velocity, angle=angle, dx=dx,
                num_results=num_results, refine_iterations=refine_iterations, seed=seed,
                shutter_offset_seconds=shutter_offset_seconds, workers=workers, force=force,
                remove_catalogs_after=remove_catalogs,
            )
            click.echo(summary.format())
