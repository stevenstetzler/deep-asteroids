import click

from deep_asteroids.cli import main
from deep_asteroids.database.search import parse_band


def _selection(f):
    for option in reversed([
        click.option("--night", "nights", multiple=True, type=int, help="Only these nights (repeatable)"),
        click.option("--run-id", "run_ids", multiple=True, type=str, help="Only these search runs (repeatable)"),
        click.option("--band", "band", type=str, default=None,
                     help="Only searches of this band (e.g. VR), or '' / none for the undefined-band (all-band, legacy) searches; default: every band"),
        click.option("--authoritative-only/--all-results", default=True, show_default=True,
                     help="Count distinct objects (the authoritative result of each de-duplication cluster), or every result"),
    ]):
        f = option(f)
    return f


def _thresholds(f):
    for option in reversed([
        click.option("--submitted-radius-arcsec", type=float, default=2.0, show_default=True),
        click.option("--synthetic-radius-arcsec", type=float, default=2.0, show_default=True),
        click.option("--known-radius-arcsec", type=float, default=2.0, show_default=True),
        click.option("--sigma-v-threshold", type=float, default=0.00025, show_default=True,
                     help="Putative real: sigma_v = hypot(sigma_vra, sigma_vdec) below this"),
    ]):
        f = option(f)
    return f


def make_thresholds(submitted_radius_arcsec, synthetic_radius_arcsec, known_radius_arcsec, sigma_v_threshold):
    from deep_asteroids.crossmatch.classify import Thresholds

    return Thresholds(submitted_radius_arcsec=submitted_radius_arcsec, synthetic_radius_arcsec=synthetic_radius_arcsec,
                      known_radius_arcsec=known_radius_arcsec, sigma_v_threshold=sigma_v_threshold)


def classified(session, nights, run_ids, thresholds, band=Ellipsis):
    """The selected results, loaded and classified, or a ClickException saying what to run first."""
    from deep_asteroids.crossmatch.classify import MissingStatisticsError, classify, load_results

    try:
        rows = load_results(session, nights=nights or None, run_ids=run_ids or None, band=band)
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
def residuals(ctx, out, nights, run_ids, band, authoritative_only, **kw):
    """Histograms of log10(median residual) by group (a diagnostic; not the putative-real cut -- see `sigma-v`)."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db
    from deep_asteroids.viz.plots import plot_residuals

    thresholds = make_thresholds(**kw)
    with Session(create_db()) as session:
        rows = classified(session, nights, run_ids, thresholds, band=parse_band(band))
    click.echo(f"wrote {plot_residuals(rows, out, authoritative_only)}")


@plot.command()
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="PNG to write")
@_selection
@_thresholds
@click.pass_context
def sigma_v(ctx, out, nights, run_ids, band, authoritative_only, **kw):
    """Histograms of log10(sigma_v) by group, with the putative-real cut marked."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db
    from deep_asteroids.viz.plots import plot_sigma_v

    thresholds = make_thresholds(**kw)
    with Session(create_db()) as session:
        rows = classified(session, nights, run_ids, thresholds, band=parse_band(band))
    click.echo(f"wrote {plot_sigma_v(rows, out, thresholds, authoritative_only)}")


@plot.command()
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="PNG to write")
@click.option("--bin-width", type=float, default=0.5, show_default=True)
@_selection
@_thresholds
@click.pass_context
def magnitudes(ctx, out, bin_width, nights, run_ids, band, authoritative_only, **kw):
    """Histograms of each result's median magnitude, by group."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db
    from deep_asteroids.viz.plots import plot_magnitudes

    thresholds = make_thresholds(**kw)
    with Session(create_db()) as session:
        rows = classified(session, nights, run_ids, thresholds, band=parse_band(band))
    click.echo(f"wrote {plot_magnitudes(rows, out, thresholds, authoritative_only, bin_width)}")


@plot.command()
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="PNG to write")
@click.option("--source", type=click.Choice(["synthetic", "mpchecker"]), default="synthetic", show_default=True)
@click.option("--radius-arcsec", type=float, default=None, show_default="2 for synthetic and mpchecker",
              help="An object is recovered if a result matches it within this mean separation")
@click.option("--min-exposure-fraction", type=float, default=0.5, show_default=True,
              help="An object is findable if it is on a searched chip for at least this fraction of the search's exposures")
@click.option("--bin-width", type=float, default=0.5, show_default=True)
@click.option("--night", "nights", multiple=True, type=int, help="Only these nights (repeatable)")
@click.option("--run-id", "run_ids", multiple=True, type=str, help="Only these search runs (repeatable)")
@click.option("--band", "band", type=str, default=None,
              help="Only searches of this band (e.g. VR), or '' / none for the undefined-band (all-band, legacy) searches; default: every band")
@click.pass_context
def completeness(ctx, out, source, radius_arcsec, min_exposure_fraction, bin_width, nights, run_ids, band):
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
                                 radius=radius, min_exposure_fraction=min_exposure_fraction, band=parse_band(band))
    if not rows:
        raise click.ClickException(f"no findable {source} objects (are there searches, results and tracklets for the night?)")
    click.echo(f"{len(rows):,} findable {source} objects, {sum(r.recovered for r in rows):,} recovered")
    click.echo(f"wrote {plot_completeness(rows, out, source, bin_width, radius)}")


@main.command("missed-objects")
@click.option("--source", type=click.Choice(["synthetic", "mpchecker", "mpc_linked"]), default="synthetic",
              show_default=True, help="mpc_linked: submitted MPC observations (#28's linked tracklets)")
@click.option("--radius-arcsec", type=float, default=None,
              show_default="2 for every source",
              help="An object is recovered if a result matches it within this mean separation")
@click.option("--min-exposure-fraction", type=float, default=0.5, show_default=True,
              help="An object is findable if it is on a searched chip for at least this fraction of the search's exposures")
@click.option("--night", "nights", multiple=True, type=int, help="Only these nights (repeatable)")
@click.option("--run-id", "run_ids", multiple=True, type=str, help="Only these search runs (repeatable)")
@click.option("--band", "band", type=str, default=None,
              help="Only searches of this band (e.g. VR), or '' / none for the undefined-band (all-band, legacy) searches; default: every band")
@click.option("--out", type=click.Path(dir_okay=False), default=None,
              help="CSV to write, one row per missed object; omit to just print the count")
@click.pass_context
def missed_objects(ctx, source, radius_arcsec, min_exposure_fraction, nights, run_ids, band, out):
    """Known objects (#40) that a search could have found but no result matched: mpchecker
    ephemerides, synthetic objects, or submitted MPC observations (mpc_linked).

    An object counts only if it was on a searched chip and inside the search's velocity range
    (see `plot completeness`); this lists the ones among those that were NOT recovered, each with
    its identity (provid/permid/name/designations, or the synthetic orbit). Needs the cross-match."""
    from sqlalchemy.orm import Session

    from deep_asteroids.crossmatch.completeness import DEFAULT_RADII, missed_known_objects, write_missed_csv
    from deep_asteroids.database import create_db

    radius = DEFAULT_RADII[source] if radius_arcsec is None else radius_arcsec
    with Session(create_db()) as session:
        missed = missed_known_objects(session, source=source, nights=nights or None, run_ids=run_ids or None,
                                      radius=radius, min_exposure_fraction=min_exposure_fraction, band=parse_band(band))
    click.echo(f"{len(missed):,} missed {source} object(s)")
    for m in missed[:20] if out is None else []:
        mag = "" if m.mag != m.mag else f", mag {m.mag:.2f}"  # mag != mag: NaN (mpc_linked)
        click.echo(f"  {m.label} -- night {m.night}, {m.run_id}{mag}")
    if out is not None:
        click.echo(f"wrote {write_missed_csv(missed, out)}")
    elif len(missed) > 20:
        click.echo(f"  ... and {len(missed) - 20} more (--out to write the full list)")


@main.command()
@_selection
@_thresholds
@click.pass_context
def summary(ctx, nights, run_ids, band, authoritative_only, **kw):
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
        rows = classified(session, nights, run_ids, thresholds, band=parse_band(band))
    click.echo(funnel(rows).format())
    kept = retained(rows)
    click.echo("  the cut keeps: " + ", ".join(
        f"{g} {n}/{total} ({n / total:.0%})" for g, (n, total) in kept.items() if g in MATCH_GROUPS and total))
    rejected = why_rejected(rows)
    if rejected:
        click.echo("  results failing each cut: " + ", ".join(f"{cut} {n:,}" for cut, n in sorted(rejected.items())))
