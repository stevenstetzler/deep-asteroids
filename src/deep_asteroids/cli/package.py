import click

from deep_asteroids.cli import main


@main.command()
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
@click.pass_context
def package(ctx, run_ids, night, field_, detector_number, band, result_nums, web_dir, product_type, cutout_size,
           annotate, force):
    """Write the static result site: one cached page per result under
    web/results/<night>/<field>/ccd<detector_number>/<band>/<result_num>/index.html (#27).

    Selects search definitions the same way `search run` does (--run-id, or
    --night/--field/--detector-number/--band); every one of their results is packaged
    unless --result-num narrows it. A page that already exists is left alone unless
    --force. Needs the same things `viz result` does: `ingest searches`/`ingest images`/
    `search run` already done, and the vendored cutout engine built."""
    from find_asteroids.models import Result, Search
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.search.run import resolve_search_definitions
    from deep_asteroids.util.cutout import LSSTCutoutPhotometry
    from deep_asteroids.web.site import package_result

    def _band(value):
        if value is None:
            return Ellipsis
        return None if value.lower() in ("", "none") else value

    if not (run_ids or night is not None or field_ or detector_number is not None):
        raise click.UsageError("select at least one search definition (--run-id, or --night/--field/--detector-number)")

    engine = LSSTCutoutPhotometry()
    n_rendered = n_skipped = n_missing = 0
    with Session(get_db()) as session:
        definitions = resolve_search_definitions(
            session, run_ids=run_ids, night=night, field=field_, detector_number=detector_number, band=_band(band))
        if not definitions:
            raise click.ClickException("no search definition matches (run `ingest searches` first?)")

        for sd in definitions:
            nums = result_nums or session.scalars(
                select(Result.result_num).join(Search, Search.id == Result.search_id)
                .where(Search.run_id == sd.run_id).order_by(Result.result_num)
            ).all()
            for result_num in nums:
                skipped, path = package_result(session, engine, sd, result_num, web_dir,
                                               product_type=product_type, cutout_size=cutout_size,
                                               annotate=annotate, force=force)
                if path is None:
                    n_missing += 1
                    click.echo(f"{sd.run_id} #{result_num}: no such result, skipping")
                elif skipped:
                    n_skipped += 1
                else:
                    n_rendered += 1

    click.echo(f"package: {n_rendered} page(s) rendered, {n_skipped} already present, {n_missing} missing, "
              f"under {web_dir}")
