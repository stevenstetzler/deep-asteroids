import click

from deep_asteroids.cli import main
from deep_asteroids.cli.ingest import TRACKLET_SOURCES

# the known_tracklet sources a cross-match can use: ingestable ones, plus the linked MPC tracklets
MATCH_SOURCES = {**TRACKLET_SOURCES, "mpc-linked": "mpc_linked"}


def _radius_options(f):
    for option in reversed([
        click.option("--submitted-radius-arcsec", type=float, default=2.0, show_default=True,
                     help="A result matches a submitted observation (mpc_linked or mpc_observation) below this "
                          "mean separation"),
        click.option("--known-radius-arcsec", type=float, default=10.0, show_default=True,
                     help="A result matches a known object (an mpchecker ephemeris) below this mean separation"),
        click.option("--synthetic-radius-arcsec", type=float, default=2.0, show_default=True,
                     help="A result matches a synthetic object below this mean separation"),
    ]):
        f = option(f)
    return f


def _radii(submitted, known, synthetic):
    return {"mpc_linked": submitted, "mpc_observation": submitted, "mpchecker": known, "synthetic": synthetic}


@main.command()
@click.option("--night", "nights", multiple=True, type=int, help="Only these nights (repeatable)")
@click.option("--field", "fields_", multiple=True, type=str, help="Only these fields (repeatable)")
@click.option("--run-id", "run_ids", multiple=True, type=str,
              help="Only the results of these search runs (repeatable); other runs' results are not clustered with them")
@click.option("--merge-radius-arcsec", type=float, default=2.0, show_default=True,
              help="Results whose tracklets agree on average over the exposures to within this are redetections")
@click.option("--force", is_flag=True, help="Redo pointing groups that are already de-duplicated")
@click.pass_context
def dedup(ctx, nights, fields_, run_ids, merge_radius_arcsec, force):
    """Group redetections of the same object among a night's results.

    Works per (night, field) pointing group: results whose tracklets agree
    within the merge radius form a cluster, and one member (the most
    Hough-stage points) is authoritative. Nothing is deleted or rewritten; see
    the result_dedup table. Re-running skips groups already done unless --force."""
    from sqlalchemy.orm import Session

    from deep_asteroids.crossmatch.dedup import dedup_results
    from deep_asteroids.database import create_db

    with Session(create_db()) as session:
        summary = dedup_results(session, nights=nights, fields=fields_, run_ids=run_ids,
                                merge_radius_arcsec=merge_radius_arcsec, force=force)
    click.echo(summary.format())


@main.command()
@click.argument("sources", nargs=-1, type=click.Choice(sorted(MATCH_SOURCES)))
@click.option("--night", "nights", multiple=True, type=int, help="Only these nights (repeatable)")
@click.option("--run-id", "run_ids", multiple=True, type=str, help="Only these search runs (repeatable)")
@click.option("--max-sep-arcsec", type=float, default=30.0, show_default=True,
              help="Store a candidate pair only below this mean separation over the exposures")
@click.option("--force", is_flag=True, help="Redo runs that are already cross-matched")
@_radius_options
@click.pass_context
def crossmatch(ctx, sources, nights, run_ids, max_sep_arcsec, force, submitted_radius_arcsec, known_radius_arcsec,
               synthetic_radius_arcsec):
    """Cross-match results against the known tracklets of the same night.

    SOURCES (default: mpc-linked, mpc-ephemeris, synthetic): mpc-linked
    (submitted observations, linked across trkids by `link`), mpc-observations
    (the same, unlinked: each trkid on its own), mpc-ephemeris (mpchecker
    ephemerides), synthetic (synthetic objects). Both
    tracklets are evaluated at the search's exposure times. Every pair below
    --max-sep-arcsec is stored (the crossmatch table); the printed table
    counts results whose closest candidate is within each source's radius.
    Re-running skips runs already done unless --force."""
    from sqlalchemy.orm import Session

    from deep_asteroids.crossmatch.match import DEFAULT_SOURCES, NotLinkedError, crossmatch_results, match_counts
    from deep_asteroids.database import create_db

    radii = _radii(submitted_radius_arcsec, known_radius_arcsec, synthetic_radius_arcsec)
    chosen = tuple(MATCH_SOURCES[s] for s in sources) if sources else DEFAULT_SOURCES
    with Session(create_db()) as session:
        try:
            summary = crossmatch_results(session, nights=nights, run_ids=run_ids, sources=chosen,
                                         max_sep_arcsec=max_sep_arcsec, force=force)
        except NotLinkedError as error:
            raise click.ClickException(str(error)) from error
        counts = match_counts(session, summary.runs + summary.skipped_runs, radii) \
            if summary.runs + summary.skipped_runs else None
    click.echo(summary.format(counts, radii))


@main.command()
@click.option("--night", "nights", multiple=True, type=int,
              help="Only these nights (repeatable); default: every night with MPC tracklets ingested")
@click.option("--match-radius-arcsec", type=float, default=1.0, show_default=True,
              help="MPC tracklets whose fits agree on average over the night's exposures to within this are "
                   "one object's night")
@click.option("--force", is_flag=True, help="Redo nights that are already linked")
@click.pass_context
def link(ctx, nights, match_radius_arcsec, force):
    """Link one object's MPC tracklets that were split across trkids.

    Groups the night's fitted mpc_observation tracklets whose fits agree over
    the night's exposures, and refits each group over all its observations
    (from deep-db). The groups are known_tracklet rows with source mpc_linked;
    known_tracklet_member ties each to the trkids it was built from. Storing
    them invalidates the night's stored cross-matches. Re-running skips nights
    already linked unless --force."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from deep_asteroids.database import create_db, deep_db_sha, get_deep_db
    from deep_asteroids.database.tracklets import KnownTracklet
    from deep_asteroids.ingest.link import link_mpc_tracklets

    with Session(create_db()) as session:
        chosen = list(nights) or sorted(session.scalars(select(KnownTracklet.night).where(
            KnownTracklet.source == "mpc_observation").distinct()))
        if not chosen:
            raise click.ClickException("no MPC tracklets ingested (run `ingest tracklets mpc-observations` first)")
        summary = link_mpc_tracklets(session, get_deep_db(), chosen, match_radius_arcsec=match_radius_arcsec,
                                     force=force, deep_db_sha=deep_db_sha())
    click.echo(summary.format())


@main.command()
@click.option("--night", "nights", multiple=True, type=int, help="Only these nights (repeatable)")
@click.option("--run-id", "run_ids", multiple=True, type=str, help="Only these search runs (repeatable)")
@click.option("--force", is_flag=True, help="Redo runs that already have statistics")
@click.pass_context
def statistics(ctx, nights, run_ids, force):
    """Compute per-result summary statistics (the result_statistics table).

    For each result: the median residual of its gathered points against its
    own tracklet, the number of outliers, its speed and velocity uncertainty,
    and the median magnitude of its points. Needs no cross-match. Re-running
    skips runs already done unless --force."""
    from sqlalchemy.orm import Session

    from deep_asteroids.crossmatch.statistics import compute_result_statistics
    from deep_asteroids.database import create_db

    with Session(create_db()) as session:
        summary = compute_result_statistics(session, nights=nights, run_ids=run_ids, force=force)
    click.echo(summary.format())
