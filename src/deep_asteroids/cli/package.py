import click

from deep_asteroids.cli import main


@main.group()
@click.pass_context
def package(ctx):
    """Write the static result site (#27): result pages (`package result`) and the
    index pages above them (`package index`)."""


@package.command()
@click.option("--run-id", "run_ids", multiple=True, help="A search definition's run_id (repeatable)")
@click.option("--night", type=int)
@click.option("--field", "field_", type=str)
@click.option("--detector-number", type=int)
@click.option("--band", type=str, help="A band, or '' / none for the undefined-band (all-band) rows")
@click.option("--result-num", "result_nums", multiple=True, type=int,
              help="Only these result numbers (repeatable); default: every result of each selected search")
@click.option("--web-dir", type=click.Path(file_okay=False), default="web", show_default=True)
@click.option("--product-type", default="differenceExp", show_default=True)
@click.option("--cutout-size", type=int, default=51, show_default=True)
@click.option("--annotate", type=str, default=None,
              help="A str.format() template for each movie frame -- see `viz result --help`")
@click.option("--force", is_flag=True, help="Re-render pages that already exist")
@click.option("--putative-real", is_flag=True,
              help="Only results that pass the putative-real cut (same classification as "
                   "`query results --putative-real`); needs `statistics`/`crossmatch` already run")
@click.option("--sigma-v-threshold", type=float, default=0.00025, show_default=True,
              help="Putative-real cut threshold, same as `query results`")
@click.option("--dry-run", is_flag=True,
              help="Report how many selected pages are already rendered vs. pending; render nothing")
@click.option("--movie/--no-movie", default=True, show_default=True,
              help="movie.gif is by far the largest artifact (#32/#33's storage estimate) -- "
                   "--no-movie skips it; the page and PDF are unaffected either way")
@click.pass_context
def result(ctx, run_ids, night, field_, detector_number, band, result_nums, web_dir, product_type, cutout_size,
          annotate, force, putative_real, sigma_v_threshold, dry_run, movie):
    """One cached page per result under
    web/results/<night>/<field>/ccd<detector_number>/<band>/<result_num>/index.html.

    Selects search definitions the same way `search run` does (--run-id, or
    --night/--field/--detector-number/--band); every one of their results is packaged
    unless --result-num or --putative-real narrows it. A page that already exists is left
    alone unless --force. Needs the same things `viz result` does: `ingest searches`/
    `ingest images`/`search run` already done, and the vendored cutout engine built
    (--dry-run excepted: it needs neither). #32's batch backfill is just this command run
    once per selected run_id, in parallel -- the already-rendered-index.html check above is
    the whole manifest, so a re-run after a partial/interrupted batch only (re)does the gap."""
    from find_asteroids.models import Result, Search
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.search.run import resolve_search_definitions
    from deep_asteroids.web.site import package_result, result_dir

    def _band(value):
        if value is None:
            return Ellipsis
        return None if value.lower() in ("", "none") else value

    if not (run_ids or night is not None or field_ or detector_number is not None):
        raise click.UsageError("select at least one search definition (--run-id, or --night/--field/--detector-number)")

    engine = None
    if not dry_run:
        from deep_asteroids.util.cutout import LSSTCutoutPhotometry

        engine = LSSTCutoutPhotometry()

    n_rendered = n_skipped = n_missing = n_pending = 0
    with Session(get_db()) as session:
        definitions = resolve_search_definitions(
            session, run_ids=run_ids, night=night, field=field_, detector_number=detector_number, band=_band(band))
        if not definitions:
            raise click.ClickException("no search definition matches (run `ingest searches` first?)")

        putative_real_nums = None
        if putative_real:
            from deep_asteroids.cli.plot import classified, make_thresholds

            thresholds = make_thresholds(2.0, 2.0, 10.0, sigma_v_threshold)
            rows = classified(session, nights=None, run_ids=[sd.run_id for sd in definitions], thresholds=thresholds)
            putative_real_nums = {}
            for r in rows:
                if r.putative_real:
                    putative_real_nums.setdefault(r.run_id, set()).add(r.result_num)

        for sd in definitions:
            nums = result_nums or session.scalars(
                select(Result.result_num).join(Search, Search.id == Result.search_id)
                .where(Search.run_id == sd.run_id).order_by(Result.result_num)
            ).all()
            if putative_real:
                nums = [n for n in nums if n in putative_real_nums.get(sd.run_id, ())]

            for result_num in nums:
                if dry_run:
                    if (result_dir(web_dir, sd, result_num) / "index.html").exists():
                        n_skipped += 1
                    else:
                        n_pending += 1
                    continue

                skipped, path = package_result(session, engine, sd, result_num, web_dir,
                                               product_type=product_type, cutout_size=cutout_size,
                                               annotate=annotate, force=force, movie=movie)
                if path is None:
                    n_missing += 1
                    click.echo(f"{sd.run_id} #{result_num}: no such result, skipping")
                elif skipped:
                    n_skipped += 1
                else:
                    n_rendered += 1

    if dry_run:
        click.echo(f"package result --dry-run: {n_skipped} already rendered, {n_pending} pending, under {web_dir}")
    else:
        click.echo(f"package result: {n_rendered} page(s) rendered, {n_skipped} already present, {n_missing} missing, "
                  f"under {web_dir}")


@package.command()
@click.option("--web-dir", type=click.Path(file_okay=False), default="web", show_default=True)
@click.pass_context
def index(ctx, web_dir):
    """(Re)write every index page above the leaf: nights -> fields -> CCDs ->
    bands (searches) -> results, straight from the database.

    Always rewrites every page (cheap: one aggregate query each), reflecting whatever's
    currently ingested -- `ingest searches`/`search run`/`ingest results` -- whether or not
    `package result` has actually rendered a given result's own page yet (its links just
    won't resolve until it has)."""
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.web.index import build_site_index

    with Session(get_db()) as session:
        paths = build_site_index(session, web_dir)
    click.echo(f"package index: {len(paths)} page(s) written under {web_dir}")
