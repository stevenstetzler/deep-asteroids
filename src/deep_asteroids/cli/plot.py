import click

from deep_asteroids.cli import main


def _selection(f):
    for option in reversed([
        click.option("--night", "nights", multiple=True, type=int, help="Only these nights (repeatable)"),
        click.option("--run-id", "run_ids", multiple=True, type=str, help="Only these search runs (repeatable)"),
        click.option("--authoritative-only/--all-results", default=True, show_default=True,
                     help="Count distinct objects (the authoritative result of each de-duplication cluster), or every result"),
    ]):
        f = option(f)
    return f


def _thresholds(f):
    for option in reversed([
        click.option("--submitted-radius-arcsec", type=float, default=2.0, show_default=True),
        click.option("--synthetic-radius-arcsec", type=float, default=2.0, show_default=True),
        click.option("--known-radius-arcsec", type=float, default=10.0, show_default=True),
        click.option("--sigma-v-threshold", type=float, default=0.00025, show_default=True,
                     help="Putative real: sigma_v = hypot(sigma_vra, sigma_vdec) below this"),
    ]):
        f = option(f)
    return f


def make_thresholds(submitted_radius_arcsec, synthetic_radius_arcsec, known_radius_arcsec, sigma_v_threshold):
    from deep_asteroids.crossmatch.classify import Thresholds

    return Thresholds(submitted_radius_arcsec=submitted_radius_arcsec, synthetic_radius_arcsec=synthetic_radius_arcsec,
                      known_radius_arcsec=known_radius_arcsec, sigma_v_threshold=sigma_v_threshold)


def classified(session, nights, run_ids, thresholds):
    """The selected results, loaded and classified, or a ClickException saying what to run first."""
    from deep_asteroids.crossmatch.classify import MissingStatisticsError, classify, load_results

    try:
        rows = load_results(session, nights=nights or None, run_ids=run_ids or None)
    except MissingStatisticsError as error:
        raise click.ClickException(str(error)) from error
    if not rows:
        raise click.ClickException("no results selected")
    return classify(rows, thresholds)


@main.group()
@click.pass_context
def plot(ctx):
    """Population plots of a night's results: residuals, sigma_v, magnitudes, completeness."""


@plot.command()
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="PNG to write")
@_selection
@_thresholds
@click.pass_context
def residuals(ctx, out, nights, run_ids, authoritative_only, **kw):
    """Histograms of log10(median residual) by group (a diagnostic; not the putative-real cut -- see `sigma-v`)."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db
    from deep_asteroids.viz.plots import plot_residuals

    thresholds = make_thresholds(**kw)
    with Session(create_db()) as session:
        rows = classified(session, nights, run_ids, thresholds)
    click.echo(f"wrote {plot_residuals(rows, out, authoritative_only)}")


@plot.command()
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="PNG to write")
@_selection
@_thresholds
@click.pass_context
def sigma_v(ctx, out, nights, run_ids, authoritative_only, **kw):
    """Histograms of log10(sigma_v) by group, with the putative-real cut marked."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db
    from deep_asteroids.viz.plots import plot_sigma_v

    thresholds = make_thresholds(**kw)
    with Session(create_db()) as session:
        rows = classified(session, nights, run_ids, thresholds)
    click.echo(f"wrote {plot_sigma_v(rows, out, thresholds, authoritative_only)}")


@plot.command()
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="PNG to write")
@click.option("--bin-width", type=float, default=0.5, show_default=True)
@_selection
@_thresholds
@click.pass_context
def magnitudes(ctx, out, bin_width, nights, run_ids, authoritative_only, **kw):
    """Histograms of each result's median magnitude, by group."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db
    from deep_asteroids.viz.plots import plot_magnitudes

    thresholds = make_thresholds(**kw)
    with Session(create_db()) as session:
        rows = classified(session, nights, run_ids, thresholds)
    click.echo(f"wrote {plot_magnitudes(rows, out, thresholds, authoritative_only, bin_width)}")


@plot.command()
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="PNG to write")
@click.option("--source", type=click.Choice(["synthetic", "mpchecker"]), default="synthetic", show_default=True)
@click.option("--radius-arcsec", type=float, default=None, show_default="2 for synthetic, 10 for mpchecker",
              help="An object is recovered if a result matches it within this mean separation")
@click.option("--min-exposure-fraction", type=float, default=0.5, show_default=True,
              help="An object is findable if it is on a searched chip for at least this fraction of the search's exposures")
@click.option("--bin-width", type=float, default=0.5, show_default=True)
@click.option("--night", "nights", multiple=True, type=int, help="Only these nights (repeatable)")
@click.option("--run-id", "run_ids", multiple=True, type=str, help="Only these search runs (repeatable)")
@click.pass_context
def completeness(ctx, out, source, radius_arcsec, min_exposure_fraction, bin_width, nights, run_ids):
    """Recovered fraction of the findable synthetic (or known) objects against magnitude.

    An object counts only if it was on a searched chip and inside the search's velocity range; it is
    recovered if a result matched it. Needs the cross-match."""
    from sqlalchemy.orm import Session

    from deep_asteroids.crossmatch.completeness import DEFAULT_RADII, completeness_rows
    from deep_asteroids.database import create_db
    from deep_asteroids.viz.plots import plot_completeness

    radius = DEFAULT_RADII[source] if radius_arcsec is None else radius_arcsec
    with Session(create_db()) as session:
        rows = completeness_rows(session, source=source, nights=nights or None, run_ids=run_ids or None,
                                 radius=radius, min_exposure_fraction=min_exposure_fraction)
    if not rows:
        raise click.ClickException(f"no findable {source} objects (are there searches, results and tracklets for the night?)")
    click.echo(f"{len(rows):,} findable {source} objects, {sum(r.recovered for r in rows):,} recovered")
    click.echo(f"wrote {plot_completeness(rows, out, source, bin_width, radius)}")


@main.command()
@_selection
@_thresholds
@click.pass_context
def summary(ctx, nights, run_ids, authoritative_only, **kw):
    """The funnel of a night's results: by what they match, and how many are putative real.

    Groups are submitted (a linked MPC tracklet within the radius), synthetic, known (an mpchecker
    ephemeris), and unmatched, split by whether the speed is inside the search's velocity range.
    Putative real is a cut on the result's own numbers (speed in range, sigma_v below threshold);
    putative new is putative real and unmatched. Needs `statistics` and `crossmatch`."""
    from sqlalchemy.orm import Session

    from deep_asteroids.crossmatch.classify import MATCH_GROUPS, funnel, retained, why_rejected
    from deep_asteroids.database import create_db

    thresholds = make_thresholds(**kw)
    with Session(create_db()) as session:
        rows = classified(session, nights, run_ids, thresholds)
    click.echo(funnel(rows).format())
    kept = retained(rows)
    click.echo("  the cut keeps: " + ", ".join(
        f"{g} {n}/{total} ({n / total:.0%})" for g, (n, total) in kept.items() if g in MATCH_GROUPS and total))
    rejected = why_rejected(rows)
    if rejected:
        click.echo("  results failing each cut: " + ", ".join(f"{cut} {n:,}" for cut, n in sorted(rejected.items())))
