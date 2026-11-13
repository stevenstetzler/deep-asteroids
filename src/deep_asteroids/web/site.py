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
`viz/result.py`, a pre-rendered one-page PDF report (``render_result_pdf()``),
and a small HTML page linking both (``render_result_page()``), and running it
again skips any page that already exists (its own cache) unless ``--force``.
"""
import html
import math
from pathlib import Path

from find_asteroids.models import Result, Search, Tracklet
from sqlalchemy import select

from deep_asteroids.viz.result import render_sky_plot, visualize_result
from deep_asteroids.viz.tracklet import render_coadd_plot, render_light_curve_plot

_ARTIFACT_LABELS = (
    ("sky", "Sky"),
    ("coadd", "Coadd"),
    ("light_curve", "Light curve"),
    ("movie", "Movie"),
)

_DEFAULT_IMG_HEIGHT_PX = 220
_COLUMN_MIN_WIDTH_PX = 220

# the one-page PDF report's layout, in inches -- see render_result_pdf(). A full US letter
# page (11x8.5) with a 0.5in margin on every side, leaving a 10x7.5 content area: a 1in title
# band across the top, then two 3.25in-tall rows -- summary (left, 5in wide) + coadd (right,
# 3.25in square) on top, sky (left, 5in wide) + light curve (right, 5in wide) on the bottom.
_PDF_PAGE_WIDTH_IN = 11.0
_PDF_PAGE_HEIGHT_IN = 8.5
_PDF_MARGIN_IN = 0.5
_PDF_CONTENT_WIDTH_IN = _PDF_PAGE_WIDTH_IN - 2 * _PDF_MARGIN_IN
_PDF_CONTENT_HEIGHT_IN = _PDF_PAGE_HEIGHT_IN - 2 * _PDF_MARGIN_IN
_PDF_TITLE_HEIGHT_IN = 1.0
_PDF_ROW_HEIGHT_IN = 3.25
_PDF_SUMMARY_WIDTH_IN = 5.0
_PDF_SUMMARY_PADDING_IN = 0.25
_PDF_COADD_SIZE_IN = 3.25
_PDF_SKY_WIDTH_IN = 5.0
_PDF_LIGHT_CURVE_WIDTH_IN = 5.0


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


def _stats_rows(sd, result_num, summary, stats):
    """``[(label, value), ...]`` shared by the HTML table and the PDF summary panel."""
    rows = [
        ("night", sd.night), ("field", sd.field), ("detector_number", sd.detector_number),
        ("band", sd.band if sd.band else "(all)"), ("run_id", sd.run_id), ("result_num", result_num),
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
    return rows


def render_result_page(sd, run_id, result_num, summary, stats, out_dir, pdf_path=None):
    """Writes ``index.html`` in ``out_dir``, referencing the artifacts ``summary`` already
    wrote there (relative paths -- the page and its images are always deployed together).

    Plain equal-width flex columns -- summary | sky | coadd | light curve | movie -- with no
    explicit mobile breakpoint: each column has the same ``min-width``, so the browser wraps
    row to column on its own as space runs out, from a wide desktop window down to a phone.
    Every plot is set to the movie's own rendered height (``width: auto`` alongside it) so
    they all line up regardless of each plot's own aspect ratio. If ``pdf_path`` is given, a
    "Download PDF" button (top right, next to the title) links to it. Returns the path."""
    esc = html.escape
    title = f"{esc(run_id)} result {result_num}"
    table = "\n".join(
        f"      <tr><th>{esc(str(k))}</th><td>{esc(str(v))}</td></tr>"
        for k, v in _stats_rows(sd, result_num, summary, stats)
    )
    figures = "\n".join(
        f'    <figure class="col"><img src="{esc(Path(summary.artifacts[kind]).name)}" alt="{esc(label)}">'
        f"<figcaption>{esc(label)}</figcaption></figure>"
        for kind, label in _ARTIFACT_LABELS if kind in summary.artifacts
    )
    img_height = _movie_height_px(summary)
    pdf_button = (
        f'<a class="pdf-button" href="{esc(Path(pdf_path).name)}" download>Download PDF</a>'
        if pdf_path is not None else ""
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: sans-serif; margin: 2em; background: #fafafa; color: #111; }}
    .page-header {{ display: flex; justify-content: space-between; align-items: center; gap: 1em; }}
    .page-header h1 {{ margin: 0; }}
    .pdf-button {{ flex: none; padding: 0.5em 1em; background: #2563eb; color: #fff; text-decoration: none;
                  border-radius: 0.3em; font-size: 0.9em; white-space: nowrap; }}
    .pdf-button:hover {{ background: #1d4ed8; }}
    .columns {{ display: flex; flex-wrap: wrap; align-items: flex-start; gap: 1.5em; margin-top: 1.5em; }}
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
  <div class="page-header">
    <h1>{title}</h1>
    {pdf_button}
  </div>
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


def render_result_pdf(sd, run_id, result_num, summary, stats, out_dir):
    """Writes a one-page, pre-rendered ``<run_id>-result-<result_num>.pdf`` in ``out_dir``: an
    11in x 8.5in page with a 0.5in margin, a 1in title band (centered) across the top, the
    summary stats top-left (``_PDF_SUMMARY_WIDTH_IN`` x ``_PDF_ROW_HEIGHT_IN``, with
    ``_PDF_SUMMARY_PADDING_IN`` of padding), the coadd top-right (``_PDF_COADD_SIZE_IN``
    square), the sky plot bottom-left and the light curve bottom-right (both
    ``_PDF_SKY_WIDTH_IN``/``_PDF_LIGHT_CURVE_WIDTH_IN`` wide x ``_PDF_ROW_HEIGHT_IN`` tall).

    The sky/coadd/light curve plots are drawn directly onto this page's own axes (sharing
    ``summary``'s already-computed measurements/cutouts and the ``sky_plot_data``
    ``visualize_result()`` stashes on it) rather than embedding the already-rendered
    ``summary.artifacts`` images -- vector output, not a rasterized copy of them, however
    those were rendered (``png`` or ``pdf``). Needs ``sky``/``coadd``/``light_curve`` in
    ``summary.artifacts`` (so there's something to draw); returns ``None`` if any are missing
    (e.g. no cutouts survived)."""
    needed = ("sky", "coadd", "light_curve")
    if not all(kind in summary.artifacts for kind in needed):
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir = Path(out_dir)

    def axes_in(x_in, y_from_top_in, w_in, h_in):
        """An Axes placed by its box in inches, measured from the content area's top-left
        (i.e. already inset by ``_PDF_MARGIN_IN`` from the page edge)."""
        left = (_PDF_MARGIN_IN + x_in) / _PDF_PAGE_WIDTH_IN
        bottom = (_PDF_PAGE_HEIGHT_IN - _PDF_MARGIN_IN - y_from_top_in - h_in) / _PDF_PAGE_HEIGHT_IN
        return fig.add_axes([left, bottom, w_in / _PDF_PAGE_WIDTH_IN, h_in / _PDF_PAGE_HEIGHT_IN])

    fig = plt.figure(figsize=(_PDF_PAGE_WIDTH_IN, _PDF_PAGE_HEIGHT_IN))

    title_ax = axes_in(0, 0, _PDF_CONTENT_WIDTH_IN, _PDF_TITLE_HEIGHT_IN)
    title_ax.axis("off")
    title_ax.text(0.5, 0.5, f"{run_id} result {result_num}", fontsize=18, va="center", ha="center",
                  transform=title_ax.transAxes)

    pad = _PDF_SUMMARY_PADDING_IN
    summary_ax = axes_in(pad, _PDF_TITLE_HEIGHT_IN + pad, _PDF_SUMMARY_WIDTH_IN - 2 * pad,
                         _PDF_ROW_HEIGHT_IN - 2 * pad)
    summary_ax.axis("off")
    rows = _stats_rows(sd, result_num, summary, stats)
    text = "\n".join(f"{k}:  {v}" for k, v in rows)
    summary_ax.text(0.0, 1.0, text, fontsize=9, va="top", ha="left", linespacing=1.8,
                    transform=summary_ax.transAxes)

    coadd_ax = axes_in(_PDF_CONTENT_WIDTH_IN - _PDF_COADD_SIZE_IN, _PDF_TITLE_HEIGHT_IN, _PDF_COADD_SIZE_IN,
                       _PDF_COADD_SIZE_IN)
    render_coadd_plot(summary, out_dir, ax=coadd_ax)  # turns its own axis off (an image, no ticks)

    bottom_row_y = _PDF_TITLE_HEIGHT_IN + _PDF_ROW_HEIGHT_IN
    sky_ax = axes_in(0, bottom_row_y, _PDF_SKY_WIDTH_IN, _PDF_ROW_HEIGHT_IN)
    tracklet, gathered, exposure_times = summary.sky_plot_data
    render_sky_plot(run_id, result_num, tracklet, gathered, exposure_times, out_dir, ax=sky_ax)

    light_curve_ax = axes_in(_PDF_SKY_WIDTH_IN, bottom_row_y, _PDF_LIGHT_CURVE_WIDTH_IN, _PDF_ROW_HEIGHT_IN)
    render_light_curve_plot(summary, out_dir, ax=light_curve_ax)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}-result-{result_num}.pdf"
    fig.savefig(path)
    plt.close(fig)
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
    pdf_path = render_result_pdf(sd, sd.run_id, result_num, summary, stats, out_dir)
    render_result_page(sd, sd.run_id, result_num, summary, stats, out_dir, pdf_path=pdf_path)
    return False, index_path
