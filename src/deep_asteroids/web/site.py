"""
Static site generation for #27: one cached ``index.html`` per result (for
now -- the night/field/CCD/band index pages above it are follow-up work),
under a directory layout that mirrors the data model exactly:

    web/results/<night>/<field>/ccd<detector_number>/<band>/<result_num>/index.html

``<night>/<field>/ccd<detector_number>`` is the data subset (a CCD's data
over one night, independent of band -- what #26's `query coverage` already
tracks); ``<band>`` ("all" for the undefined-band union) is the search
performed on it -- today a 1:1 `search_definition`/`run_id`, but a
directory of its own in case a data subset is ever searched more than one
way; ``<result_num>`` is the result itself. No server, no database at
request time: `deep-asteroids package` renders each page's four plots with
`viz/result.py` and writes a small HTML page alongside them, and running it
again skips any page that already exists (its own cache) unless ``--force``.
"""
import html
import math
from pathlib import Path

from find_asteroids.models import Result, Search, Tracklet
from sqlalchemy import select

from deep_asteroids.viz.result import visualize_result

_ARTIFACT_LABELS = (
    ("sky", "Sky"),
    ("coadd", "Coadd"),
    ("light_curve", "Light curve"),
    ("movie", "Movie"),
)

_DEFAULT_IMG_HEIGHT_PX = 220
_COLUMN_MIN_WIDTH_PX = 220


def band_slug(band):
    """The ``<band>`` path segment for a ``search_definition.band`` value: ``"all"`` for the
    undefined-band (``None``) union, else the band itself."""
    return band if band is not None else "all"


def result_dir(web_dir, sd, result_num):
    """``web/results/<night>/<field>/ccd<detector_number>/<band>/<result_num>/``."""
    return (Path(web_dir) / "results" / str(sd.night) / sd.field / f"ccd{sd.detector_number}"
            / band_slug(sd.band) / str(result_num))


def _result_stats(session, run_id, result_num):
    """``(n, vra, vdec, sigma_v)`` for one result (``sigma_v`` is ``hypot(sigma_vra, sigma_vdec)``,
    find-asteroids' own velocity-uncertainty columns), or ``None`` if it doesn't exist."""
    row = session.execute(
        select(Result.n, Tracklet.vra, Tracklet.vdec, Tracklet.sigma_vra, Tracklet.sigma_vdec)
        .join(Search, Search.id == Result.search_id).join(Tracklet, Tracklet.result_id == Result.id)
        .where(Search.run_id == run_id, Result.result_num == result_num)
    ).one_or_none()
    if row is None:
        return None
    n, vra, vdec, sigma_vra, sigma_vdec = row
    sigma_v = math.hypot(sigma_vra, sigma_vdec) if sigma_vra is not None and sigma_vdec is not None else None
    return n, vra, vdec, sigma_v


def _movie_height_px(summary):
    """The movie's own rendered pixel height, so the other plots can be set to match it and
    everything lines up in a row -- falls back to ``_DEFAULT_IMG_HEIGHT_PX`` if there's no
    movie (e.g. no cutouts survived) or it can't be read."""
    path = summary.artifacts.get("movie")
    if path:
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as im:
                return im.height
        except OSError:
            pass
    return _DEFAULT_IMG_HEIGHT_PX


def render_result_page(sd, run_id, result_num, summary, stats, out_dir):
    """Writes ``index.html`` in ``out_dir``, referencing the artifacts ``summary`` already
    wrote there (relative paths -- the page and its images are always deployed together).

    Plain equal-width flex columns -- summary | sky | coadd | light curve | movie -- with no
    explicit mobile breakpoint: each column has the same ``min-width``, so the browser wraps
    row to column on its own as space runs out, from a wide desktop window down to a phone.
    Every plot is set to the movie's own rendered height (``width: auto`` alongside it) so
    they all line up regardless of each plot's own aspect ratio. Returns the path."""
    esc = html.escape
    title = f"{esc(run_id)} result {result_num}"
    rows = [
        ("night", sd.night), ("field", esc(sd.field)), ("detector_number", sd.detector_number),
        ("band", esc(sd.band) if sd.band else "(all)"), ("run_id", esc(run_id)), ("result_num", result_num),
    ]
    if stats is not None:
        n, vra, vdec, sigma_v = stats
        rows += [
            ("n (Hough points)", n), ("vra (deg/day)", f"{vra:.5f}"), ("vdec (deg/day)", f"{vdec:.5f}"),
            ("sigma_v (deg/day)", f"{sigma_v:.2e}" if sigma_v is not None else ""),
        ]
    rows += [
        ("exposures touched", summary.n_exposures_touched), ("images found", summary.n_images_found),
        ("measurements", len(summary.measurements)),
    ]
    table = "\n".join(f"      <tr><th>{esc(str(k))}</th><td>{v}</td></tr>" for k, v in rows)
    figures = "\n".join(
        f'    <figure class="col"><img src="{esc(Path(summary.artifacts[kind]).name)}" alt="{esc(label)}">'
        f"<figcaption>{esc(label)}</figcaption></figure>"
        for kind, label in _ARTIFACT_LABELS if kind in summary.artifacts
    )
    img_height = _movie_height_px(summary)
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: sans-serif; margin: 2em; background: #fafafa; color: #111; }}
    .columns {{ display: flex; flex-wrap: wrap; align-items: flex-start; gap: 1.5em; }}
    .col {{ flex: 1 1 {_COLUMN_MIN_WIDTH_PX}px; min-width: {_COLUMN_MIN_WIDTH_PX}px; margin: 0; }}
    .col h2 {{ font-size: 1em; font-weight: normal; color: #555; margin: 0 0 0.5em 0; }}
    .col img {{ height: {img_height}px; width: auto; max-width: 100%; display: block; margin: 0 auto;
               background: #000; }}
    figcaption {{ text-align: center; color: #555; margin-top: 0.3em; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ text-align: left; padding: 0.15em 0.6em; font-size: 0.9em; }}
    th {{ color: #555; font-weight: normal; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class="columns">
    <div class="col">
      <h2>Summary</h2>
      <table>
{table}
      </table>
    </div>
{figures}
  </div>
</body>
</html>
"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.html"
    path.write_text(page)
    return path


def package_result(session, engine, sd, result_num, web_dir, product_type="differenceExp", cutout_size=51,
                   annotate=None, force=False):
    """Renders one result's page (its four plots + ``index.html``) under
    ``web_dir``, skipping it if ``index.html`` already exists unless
    ``force``. Returns ``(skipped: bool, path_or_None)`` -- ``path`` is
    ``None`` if the result doesn't exist (e.g. it was de-duplicated away
    or never ingested)."""
    out_dir = result_dir(web_dir, sd, result_num)
    index_path = out_dir / "index.html"
    if index_path.exists() and not force:
        return True, index_path

    summary = visualize_result(session, engine, sd.run_id, result_num, out_dir, product_type=product_type,
                               cutout_size=cutout_size, annotate=annotate)
    if summary is None:
        return False, None
    stats = _result_stats(session, sd.run_id, result_num)
    render_result_page(sd, sd.run_id, result_num, summary, stats, out_dir)
    return False, index_path
