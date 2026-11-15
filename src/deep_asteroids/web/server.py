"""
A live, per-request alternative to the static site (``web/index.py``) -- the same
nights -> fields -> CCDs -> bands -> results drill-down, plus the flat ``/search/``
coverage page, computed fresh on every request instead of written to thousands of files up
front.

Built to answer a concrete question that came up finishing #32/#33's full-survey PDF
backfill: once the database has ~44k ``search_definition`` rows, bulk-regenerating every
index page as static files (``deep-asteroids package index``) turned out to take hours --
not because any one query is expensive (none of them are: see the benchmark in
``tests/test_server.py``), but because writing tens of thousands of small files is itself a
slow, GPFS-metadata-bound operation, made much worse by page-cache contention on a shared
Slurm node. A live server sidesteps the file-writing cost entirely: each request runs one
narrow, already-indexed query and returns the page directly, nothing ever written to disk.

Sketch only -- modeled on salad's own ``serve.py`` (Flask app factory, one engine built at
startup, a fresh ``Session`` per request via Flask's ``g``/teardown pattern). Reuses
``web/index.py``'s own row-fetching functions and ``_table_html()`` verbatim, so a page here
renders byte-for-byte the same as its static counterpart -- this is a different transport for
the same data and the same HTML, not a rewrite. Leaf result pages (the ones with plots/PDFs,
``package_result()``'s own -- comparatively expensive to render, and already cached as files)
aren't reimplemented here: this sketch only covers the index/navigation layer that was
actually slow to bulk-generate.

**Benchmarked against the real full-survey database** (43,586 ``search_definition`` rows,
276,797 ``result`` rows, 3.5 GB SQLite file, on an otherwise-idle node -- no page-cache
contention this time), timing the same query functions this module calls directly:

======================================  ==========  ===========
query (scoped to one leaf)              cold         warm
======================================  ==========  ===========
``fields_rows(night)``                   227 ms       197 ms
``detectors_rows(night, field)``           9 ms         4 ms
``bands_rows(night, field, detector)``     5 ms         3 ms
``results_rows(run_id)``                   3 ms         2 ms
======================================  ==========  ===========

Every one of those is well within what a live page can afford -- and there are tens of
thousands of them, which is exactly what made bulk-writing them all as static files slow
(#32/#33's finding: the bottleneck was file-count/metadata I/O, not query cost). Serving them
live sidesteps that entirely.

The two whole-survey rollups are a different story:

======================================  ==========  ===========
query (whole survey, one row each)      cold         warm
======================================  ==========  ===========
``nights_rows()`` (64 nights)            24.8 s       15.9 s
``search_coverage_rows()`` (43,586 rows) 23.0 s       20.1 s
======================================  ==========  ===========

Both join all the way down to ``result`` (276,797 rows) to produce their counts, and that's
a genuinely large aggregation regardless of indexes (both relevant foreign keys already have
one) -- computing it fresh on every request would be a bad page load, static-vs-live aside.
**So the honest answer isn't "replace the static site with this," it's a hybrid**: serve the
drill-down (nights -> fields -> CCDs -> bands -> results) live, the way this module does, and
keep the two rollup pages (``/`` itself and ``/search/``) cached/periodic -- computed once
and refreshed on a timer or after an ingest run, the same shape of tradeoff
``package index`` already makes, just for two pages instead of tens of thousands. This
module doesn't implement that caching layer yet (`/` and `/search/` below compute fresh every
request, same as everything else) -- it's sized right for the benchmark, not for real
deployment.

Run it (bind to 127.0.0.1 only -- never 0.0.0.0 on a shared host, same convention salad's own
serve.py uses): ``deep-asteroids serve --db sqlite:///path/to/deep_asteroids.sqlite``.
"""
from flask import Flask, abort, current_app, g
from sqlalchemy.orm import Session

from deep_asteroids.web.index import (
    _table_html,
    bands_rows,
    detectors_rows,
    fields_rows,
    nights_rows,
    results_rows,
    search_coverage_rows,
)
from deep_asteroids.web.site import band_slug


def create_app(engine):
    """Flask app factory -- ``engine`` is built once by the caller (e.g.
    ``deep_asteroids.database.get_db()``) and shared across requests; each request gets its
    own ``Session``, opened lazily and closed in ``teardown_appcontext`` (same convention as
    salad's ``serve.py``)."""
    app = Flask(__name__)
    app.config["ENGINE"] = engine

    @app.teardown_appcontext
    def _close_session(exception=None):
        session = g.pop("db_session", None)
        if session is not None:
            session.close()

    def _session():
        if "db_session" not in g:
            g.db_session = Session(current_app.config["ENGINE"])
        return g.db_session

    @app.route("/")
    def index():
        rows = nights_rows(_session())
        return _table_html(
            "deep-asteroids: nights", None,
            ["night", "fields", "searches defined", "searches executed", "results"],
            [((night, f"/results/{night}/"), n_fields, n_defs, n_searched, n_results)
             for night, n_fields, n_defs, n_searched, n_results in rows],
            "No nights ingested yet (run `ingest searches` first).",
            extra_links=[("search coverage (every search_definition, flat, whole survey)", "/search/")])

    @app.route("/search/")
    def search_coverage():
        rows = search_coverage_rows(_session())
        return _table_html(
            "deep-asteroids: search coverage", "/",
            ["(night, field, detector)", "band", "n_exposures", "n_images", "searched", "results"],
            [((f"{night} / {field} / ccd{detector_number}", f"/results/{night}/{field}/ccd{detector_number}/"),
              band if band else "(all)", n_exposures, n_images, "yes" if searched else "no", n_results)
             for night, field, detector_number, band, run_id, n_exposures, n_images, searched, n_results in rows],
            "No search definitions ingested yet (run `ingest searches` first).")

    @app.route("/results/<int:night>/")
    def fields(night):
        rows = fields_rows(_session(), night)
        if not rows:
            abort(404)
        return _table_html(
            f"{night}: fields", "/",
            ["field", "detectors", "searches defined", "searches executed", "results"],
            [((field, f"/results/{night}/{field}/"), n_detectors, n_defs, n_searched, n_results)
             for field, n_detectors, n_defs, n_searched, n_results in rows],
            "No fields ingested yet.")

    @app.route("/results/<int:night>/<field>/")
    def detectors(night, field):
        rows = detectors_rows(_session(), night, field)
        if not rows:
            abort(404)
        return _table_html(
            f"{night}/{field}: CCDs", f"/results/{night}/",
            ["detector", "detector_name", "searches defined", "searches executed", "results"],
            [((f"ccd{detector_number}", f"/results/{night}/{field}/ccd{detector_number}/"), detector_name,
              n_defs, n_searched, n_results)
             for detector_number, detector_name, n_defs, n_searched, n_results in rows],
            "No CCDs ingested yet.")

    @app.route("/results/<int:night>/<field>/ccd<int:detector_number>/")
    def bands(night, field, detector_number):
        rows = bands_rows(_session(), night, field, detector_number)
        if not rows:
            abort(404)
        return _table_html(
            f"{night}/{field}/ccd{detector_number}: searches", f"/results/{night}/{field}/",
            ["band", "run_id", "n_exposures", "searched", "results", "engine", "vmin (deg/day)",
             "vmax (deg/day)", "angle_min (deg)", "angle_max (deg)", "dx (arcsec)", "snr"],
            [((band if band else "(all)", f"/results/{night}/{field}/ccd{detector_number}/{band_slug(band)}/"),
              run_id, n_exposures, "yes" if searched else "no", n_results, "find-asteroids" if searched else "",
              f"{vmin:.3f}" if vmin is not None else "", f"{vmax:.3f}" if vmax is not None else "",
              f"{angle_min:.2f}" if angle_min is not None else "",
              f"{angle_max:.2f}" if angle_max is not None else "", f"{dx:.1f}" if dx is not None else "",
              f"{snr:.1f}" if snr is not None else "")
             for band, run_id, n_exposures, searched, n_results, vmin, vmax, angle_min, angle_max, dx, snr in rows],
            "No searches ingested yet.")

    @app.route("/results/<int:night>/<field>/ccd<int:detector_number>/<band_path>/")
    def results(night, field, detector_number, band_path):
        band_rows = bands_rows(_session(), night, field, detector_number)
        match = next((r for r in band_rows if band_slug(r[0]) == band_path), None)
        if match is None:
            abort(404)
        run_id = match[1]
        rows = results_rows(_session(), run_id)
        return _table_html(
            f"{run_id}: results", f"/results/{night}/{field}/ccd{detector_number}/",
            ["result_num", "n", "vra (deg/day)", "vdec (deg/day)", "sigma_v (deg/day)"],
            [((result_num, f"/results/{night}/{field}/ccd{detector_number}/{band_path}/{result_num}/"),
              n, f"{vra:.5f}", f"{vdec:.5f}", f"{sigma_v:.2e}" if sigma_v is not None else "")
             for result_num, n, vra, vdec, sigma_v in rows],
            "No results ingested yet (run `search run`/`ingest results` first).")

    return app
