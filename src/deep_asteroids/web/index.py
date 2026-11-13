"""
Index pages above the leaf result page (#27 follow-up): plain tables of
metadata, each linking down to its child pages, built straight from the
current database state --

    nights -> fields -> CCDs -> bands (searches) -> results

-- down to the per-result page `web/site.py`'s ``package_result()``
already builds. No server, no JS, no pagination: every level's rows come
back pre-sorted by a sensible default column (numeric night/detector
number, lexical field/band, numeric result_num -- find-asteroids' own
ranking, best first). Unlike the leaf result pages, an index page's cost
is one aggregate query, so `deep-asteroids package index` always rewrites
every one of them rather than caching/skipping.
"""
import html
import math
from pathlib import Path

from find_asteroids.models import Result, Search, Tracklet
from sqlalchemy import func, select

from deep_asteroids.database.search import SearchDefinition as SD
from deep_asteroids.web.site import band_slug


def nights_rows(session):
    """``[(night, n_fields, n_definitions, n_results), ...]``, by night."""
    return session.execute(
        select(SD.night, func.count(func.distinct(SD.field)), func.count(func.distinct(SD.id)),
               func.count(Result.id))
        .select_from(SD).outerjoin(Search, Search.run_id == SD.run_id).outerjoin(Result, Result.search_id == Search.id)
        .group_by(SD.night).order_by(SD.night)
    ).all()


def fields_rows(session, night):
    """``[(field, n_detectors, n_definitions, n_results), ...]`` for one night, by field."""
    return session.execute(
        select(SD.field, func.count(func.distinct(SD.detector_number)), func.count(func.distinct(SD.id)),
               func.count(Result.id))
        .select_from(SD).outerjoin(Search, Search.run_id == SD.run_id).outerjoin(Result, Result.search_id == Search.id)
        .where(SD.night == night).group_by(SD.field).order_by(SD.field)
    ).all()


def detectors_rows(session, night, field):
    """``[(detector_number, detector_name, n_definitions, n_results), ...]`` for one
    (night, field), by detector_number."""
    return session.execute(
        select(SD.detector_number, func.min(SD.detector_name), func.count(func.distinct(SD.id)),
               func.count(Result.id))
        .select_from(SD).outerjoin(Search, Search.run_id == SD.run_id).outerjoin(Result, Result.search_id == Search.id)
        .where(SD.night == night, SD.field == field)
        .group_by(SD.detector_number).order_by(SD.detector_number)
    ).all()


def bands_rows(session, night, field, detector_number):
    """``[(band, run_id, n_exposures, searched, n_results), ...]`` for one
    (night, field, detector_number) -- one row per `search_definition` -- by band (``None``,
    the undefined-band union, sorts by its slug, "all"). ``searched`` is whether a
    find-asteroids `search` row exists for this `run_id` (not `search_run`: most of this
    database's results were copied in from another results database, with no `search_run`
    row of their own -- see `ingest results`)."""
    rows = session.execute(
        select(SD.band, SD.run_id, SD.n_exposures, Search.id, func.count(Result.id))
        .select_from(SD).outerjoin(Search, Search.run_id == SD.run_id).outerjoin(Result, Result.search_id == Search.id)
        .where(SD.night == night, SD.field == field, SD.detector_number == detector_number)
        .group_by(SD.id)
    ).all()
    return sorted(
        ((band, run_id, n_exposures, search_id is not None, n_results)
         for band, run_id, n_exposures, search_id, n_results in rows),
        key=lambda r: band_slug(r[0]),
    )


def results_rows(session, run_id):
    """``[(result_num, n, vra, vdec, sigma_v), ...]`` for one run_id, by result_num
    (find-asteroids' own ranking, best first)."""
    rows = session.execute(
        select(Result.result_num, Result.n, Tracklet.vra, Tracklet.vdec, Tracklet.sigma_vra, Tracklet.sigma_vdec)
        .join(Search, Search.id == Result.search_id).join(Tracklet, Tracklet.result_id == Result.id)
        .where(Search.run_id == run_id).order_by(Result.result_num)
    ).all()
    return [
        (result_num, n, vra, vdec, math.hypot(sigma_vra, sigma_vdec) if sigma_vra is not None else None)
        for result_num, n, vra, vdec, sigma_vra, sigma_vdec in rows
    ]


def _render_table_page(out_path, title, up_href, columns, rows, empty_message):
    """Writes a plain HTML page: an optional "up" link, then a table of ``rows`` under
    ``columns`` headers (or ``empty_message`` if there are none). Each row's first element
    is ``(label, href)`` for its link cell; the rest are plain values. Returns the path."""
    esc = html.escape
    out_path = Path(out_path)

    if rows:
        header = "".join(f"<th>{esc(c)}</th>" for c in columns)
        body = []
        for link_cell, *rest in rows:
            label, href = link_cell
            cells = [f'<td><a href="{esc(href)}">{esc(str(label))}</a></td>']
            cells += [f"<td>{esc(str(v))}</td>" for v in rest]
            body.append("    <tr>" + "".join(cells) + "</tr>")
        table = f"  <table>\n    <tr>{header}</tr>\n" + "\n".join(body) + "\n  </table>"
    else:
        table = f"  <p>{esc(empty_message)}</p>"
    up_link = f'  <p><a href="{esc(up_href)}">&uarr; up</a></p>\n' if up_href else ""

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    body {{ font-family: sans-serif; margin: 2em; background: #fafafa; color: #111; }}
    table {{ border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 0.3em 1em; border-bottom: 1px solid #ddd; }}
    th {{ color: #555; font-weight: normal; }}
    a {{ color: #2563eb; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>{esc(title)}</h1>
{up_link}{table}
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page)
    return out_path


def build_site_index(session, web_dir):
    """(Re)writes every index page above the leaf: ``web_dir/index.html`` (nights), then
    one page per night (fields), per (night, field) (CCDs), per (night, field,
    detector_number) (bands/searches), and per `search_definition` (results) -- straight
    from the database, always overwritten (cheap: one aggregate query each). Returns the
    list of paths written."""
    web_dir = Path(web_dir)
    paths = []

    night_rows = nights_rows(session)
    paths.append(_render_table_page(
        web_dir / "index.html", "deep-asteroids: nights", None,
        ["night", "fields", "searches", "results"],
        [((night, f"results/{night}/index.html"), n_fields, n_defs, n_results)
         for night, n_fields, n_defs, n_results in night_rows],
        "No nights ingested yet (run `ingest searches` first)."))

    for night, *_ in night_rows:
        field_rows = fields_rows(session, night)
        paths.append(_render_table_page(
            web_dir / "results" / str(night) / "index.html", f"{night}: fields", "../../index.html",
            ["field", "detectors", "searches", "results"],
            [((field, f"{field}/index.html"), n_detectors, n_defs, n_results)
             for field, n_detectors, n_defs, n_results in field_rows],
            "No fields ingested yet."))

        for field, *_ in field_rows:
            detector_rows = detectors_rows(session, night, field)
            paths.append(_render_table_page(
                web_dir / "results" / str(night) / field / "index.html", f"{night}/{field}: CCDs",
                "../index.html", ["detector", "detector_name", "searches", "results"],
                [((f"ccd{detector_number}", f"ccd{detector_number}/index.html"), detector_name, n_defs, n_results)
                 for detector_number, detector_name, n_defs, n_results in detector_rows],
                "No CCDs ingested yet."))

            for detector_number, *_ in detector_rows:
                band_rows = bands_rows(session, night, field, detector_number)
                ccd_dir = web_dir / "results" / str(night) / field / f"ccd{detector_number}"
                paths.append(_render_table_page(
                    ccd_dir / "index.html", f"{night}/{field}/ccd{detector_number}: searches", "../index.html",
                    ["band", "run_id", "n_exposures", "searched", "results"],
                    [((band if band else "(all)", f"{band_slug(band)}/index.html"), run_id, n_exposures,
                      "yes" if searched else "no", n_results)
                     for band, run_id, n_exposures, searched, n_results in band_rows],
                    "No searches ingested yet."))

                for band, run_id, _n_exposures, _searched, _n_results in band_rows:
                    result_dir = ccd_dir / band_slug(band)
                    rows = results_rows(session, run_id)
                    paths.append(_render_table_page(
                        result_dir / "index.html", f"{run_id}: results", "../index.html",
                        ["result_num", "n", "vra (deg/day)", "vdec (deg/day)", "sigma_v (deg/day)"],
                        [((result_num, f"{result_num}/index.html"), n, f"{vra:.5f}", f"{vdec:.5f}",
                          f"{sigma_v:.2e}" if sigma_v is not None else "")
                         for result_num, n, vra, vdec, sigma_v in rows],
                        "No results ingested yet (run `search run`/`ingest results` first)."))
    return paths
