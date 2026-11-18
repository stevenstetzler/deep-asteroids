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


_MERGE_GROUPS = ("mpchecker", "synthetic", "putative-new")


@package.command("merge-pdfs")
@click.option("--night", "nights", multiple=True, type=int,
              help="Only these nights (repeatable); default: every night with a search definition")
@click.option("--group", "group_names", multiple=True, type=click.Choice(_MERGE_GROUPS),
              help="Only these groups (repeatable); default: all three")
@click.option("--web-dir", type=click.Path(file_okay=False), default="web", show_default=True)
@click.option("--out-dir", type=click.Path(file_okay=False), default=None,
              help="default: <web-dir>/pdfs")
@click.option("--sigma-v-threshold", type=float, default=0.00015, show_default=True,
              help="Putative-real cut threshold -- must match whatever `package result "
                   "--putative-real` rendered, or the merge references PDFs that don't exist")
@click.option("--authoritative-only/--all-results", default=True, show_default=True,
              help="One page per distinct object (the authoritative result of each "
                   "de-duplication cluster), or every result including redetections")
@click.option("--force", is_flag=True, help="Re-merge (night, group) PDFs that already exist")
@click.pass_context
def merge_pdfs(ctx, nights, group_names, web_dir, out_dir, sigma_v_threshold, authoritative_only, force):
    """Merge already-rendered per-result PDFs (`package result`) into one PDF per (night,
    group) under <out-dir>/<night>/<group>.pdf -- kept per-night, not one survey-wide PDF per
    group, to keep file sizes manageable (#32/#33).

    The three groups are the ones docs/results/magnitudes.png highlights: "mpchecker" (matches
    a mpchecker ephemeris -- the KNOWN group), "synthetic" (matches a synthetic/fake object),
    and "putative-new" (unmatched and passes the putative-real cut -- a candidate no known
    object explains). A (night, group) with no matching results, or none of whose PDFs were
    actually rendered, is skipped -- no empty file written. Needs `statistics`/`crossmatch`/
    `dedup` already run, and the individual PDFs this merges already rendered by `package
    result`."""
    from pathlib import Path

    from pypdf import PdfWriter
    from sqlalchemy.orm import Session

    from deep_asteroids.cli.plot import classified, make_thresholds
    from deep_asteroids.crossmatch.classify import KNOWN, SYNTHETIC
    from deep_asteroids.database import get_db
    from deep_asteroids.search.run import resolve_search_definitions
    from deep_asteroids.web.site import result_dir

    web_dir = Path(web_dir)
    out_dir = Path(out_dir) if out_dir else web_dir / "pdfs"
    groups = group_names or _MERGE_GROUPS
    thresholds = make_thresholds(2.0, 2.0, 10.0, sigma_v_threshold)

    def matches(r, group):
        if group == "mpchecker":
            return r.group == KNOWN
        if group == "synthetic":
            return r.group == SYNTHETIC
        return r.putative_new  # "putative-new"

    n_written = n_skipped = n_empty = n_missing_pdf = 0
    with Session(get_db()) as session:
        selected_nights = nights or sorted({sd.night for sd in resolve_search_definitions(session)})
        for night in selected_nights:
            sds = {sd.run_id: sd for sd in resolve_search_definitions(session, night=night)}
            # VR run_ids still lack statistics/crossmatch/dedup; a `nights=[night]` filter would pull
            # them in too and fail the whole night with MissingStatisticsError, so scope to the
            # putative-real groups' own (non-VR) run_ids instead.
            non_vr_run_ids = [run_id for run_id in sds if not run_id.endswith("-VR")]
            if not non_vr_run_ids:
                continue
            try:
                rows = classified(session, nights=None, run_ids=non_vr_run_ids, thresholds=thresholds)
            except click.ClickException:
                continue  # no results (or none with statistics) for this night
            if authoritative_only:
                rows = [r for r in rows if r.authoritative]

            for group in groups:
                out_path = out_dir / str(night) / f"{group}.pdf"
                if out_path.exists() and not force:
                    n_skipped += 1
                    continue

                writer = PdfWriter()
                n_included = 0
                for r in rows:
                    if not matches(r, group):
                        continue
                    sd = sds.get(r.run_id)
                    if sd is None:
                        continue
                    pdf_path = result_dir(web_dir, sd, r.result_num) / f"{r.run_id}-result-{r.result_num}.pdf"
                    if not pdf_path.exists():
                        n_missing_pdf += 1
                        continue
                    writer.append(str(pdf_path))
                    n_included += 1

                if n_included == 0:
                    writer.close()
                    n_empty += 1
                    continue

                out_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = out_path.with_suffix(".pdf.tmp")
                with open(tmp_path, "wb") as f:
                    writer.write(f)
                writer.close()
                tmp_path.replace(out_path)
                n_written += 1
                click.echo(f"{night}/{group}: {n_included} page(s) -> {out_path}")

    click.echo(f"package merge-pdfs: {n_written} merged PDF(s) written, {n_skipped} already present, "
              f"{n_empty} empty (no rendered result matched), {n_missing_pdf} result(s) skipped (no PDF rendered)")
