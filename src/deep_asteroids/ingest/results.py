"""
Ingest find-asteroids results into the deep-asteroids database, from
either of the two places find-asteroids leaves them:

* **A results directory on disk** (``ingest_results_dir``): the
  ``<results_dir>/<result_num>/{result,gathered,points,tracklet}.<fmt>``
  astropy tables ``find_asteroids.search.run_search()`` writes.
* **Another find-asteroids results database** (``ingest_results_db``): a
  SQLAlchemy URL for a database holding find-asteroids' own
  ``search``/``result``/``gathered``/``points``/``tracklet`` tables (e.g.
  salad's), copied over run by run.

Rows land in find-asteroids' own tables (``search``, ``result``, ...),
which share ``Base.metadata`` with ours. A ``Search.run_id`` equal to a
``search_definition.run_id`` is what ties a run back to its (night, field,
detector) search definition -- ``ResultsSummary.unmatched_run_ids``
reports run_ids with no such definition.

Values are copied verbatim: times stay in whatever scale find-asteroids
wrote (TAI), with no correction for salad's known timestamp offset (see
the README).

Ingest is idempotent per run_id: a run_id that already has a ``search``
row is skipped, unless ``force``, which deletes its rows and re-ingests.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, insert, inspect, select
from sqlalchemy.orm import Session

from deep_asteroids.database.search import SearchDefinition
from find_asteroids.models import Gathered, Points, Result, Search, Tracklet

CHILD_MODELS = (Gathered, Points, Tracklet)  # keyed by result_id
TABLE_NAMES = ("result", "gathered", "points", "tracklet")  # results_dir file names
BATCH_SIZE = 20_000


@dataclass
class ResultsSummary:
    run_ids: list = field(default_factory=list)
    skipped_run_ids: list = field(default_factory=list)
    unmatched_run_ids: list = field(default_factory=list)
    n_results: int = 0
    n_gathered: int = 0
    n_points: int = 0
    n_tracklets: int = 0

    def format(self):
        lines = [
            f"results: {len(self.run_ids)} run(s) ingested, {len(self.skipped_run_ids)} skipped "
            "(already present; --force to replace)"
        ]
        if self.run_ids:
            lines.append(
                f"  {self.n_results:,} result(s), {self.n_gathered:,} gathered, "
                f"{self.n_points:,} point(s), {self.n_tracklets:,} tracklet(s)"
            )
        if self.unmatched_run_ids:
            shown = ", ".join(self.unmatched_run_ids[:5]) + (" ..." if len(self.unmatched_run_ids) > 5 else "")
            lines.append(
                f"  warning: {len(self.unmatched_run_ids):,} run_id(s) match no search_definition "
                f"(run `ingest searches` first?): {shown}"
            )
        return "\n".join(lines)


def run_id_night(run_id):
    """The night a run_id names, if it ends ``...-<YYYYMMDD>`` or
    ``...-<YYYYMMDD>-<band>`` (our/salad's convention), else ``None``."""
    m = re.search(r"-(\d{8})(?:-[^-]+)?$", run_id)
    return int(m.group(1)) if m else None


def _model_columns(model):
    return {c.name for c in model.__table__.columns} - {"id"}


def _clean(value):
    """A plain-Python, JSON/DB-safe version of an astropy table cell:
    numpy scalars -> Python scalars, masked -> ``None``, a ``Time`` -> MJD TAI."""
    import numpy as np
    from astropy.time import Time

    if value is np.ma.masked:
        return None
    if isinstance(value, Time):
        return float(value.tai.mjd)
    if hasattr(value, "item"):
        return value.item()
    return value


def _split_extra(row, model):
    """``row`` -> a ``model`` insert dict; columns the model lacks are
    preserved under ``extra`` (find-asteroids' own ``compile_results_db`` convention)."""
    columns = _model_columns(model)
    out = {k: v for k, v in row.items() if k in columns and k != "extra"}
    extra = dict(row.get("extra") or {}) if "extra" in columns else {}
    extra.update({str(k): v for k, v in row.items() if k not in columns})
    out["extra"] = extra
    return out


def _delete_run(session, run_id):
    search_ids = select(Search.id).where(Search.run_id == run_id)
    result_ids = select(Result.id).where(Result.search_id.in_(search_ids))
    for model in CHILD_MODELS:
        session.execute(delete(model).where(model.result_id.in_(result_ids)))
    session.execute(delete(Result).where(Result.search_id.in_(search_ids)))
    session.execute(delete(Search).where(Search.run_id == run_id))


def _claim_run_ids(session, run_ids, force):
    """``(todo, skipped)`` of ``run_ids`` given what's already ingested;
    with ``force``, existing runs are deleted first."""
    existing = set(session.scalars(select(Search.run_id).where(Search.run_id.in_(run_ids)))) if run_ids else set()
    if force:
        for run_id in existing:
            _delete_run(session, run_id)
        return list(run_ids), []
    return [r for r in run_ids if r not in existing], [r for r in run_ids if r in existing]


def _note_unmatched(session, summary):
    known = set(session.scalars(select(SearchDefinition.run_id).where(SearchDefinition.run_id.in_(summary.run_ids))))
    summary.unmatched_run_ids = [r for r in summary.run_ids if r not in known]


def _insert_results(session, search_id, result_rows):
    """Insert ``result_rows`` (dicts with ``result_num`` etc.) and return
    their new ids, in the same order."""
    ids = []
    for i in range(0, len(result_rows), BATCH_SIZE):
        batch = [{**r, "search_id": search_id} for r in result_rows[i:i + BATCH_SIZE]]
        ids.extend(session.scalars(insert(Result).returning(Result.id, sort_by_parameter_order=True), batch).all())
    return ids


def _insert_children(session, model, rows):
    for i in range(0, len(rows), BATCH_SIZE):
        session.execute(insert(model), rows[i:i + BATCH_SIZE])


def ingest_results_dir(session, results_dir, run_id, output_format="ecsv", force=False, params=None):
    """Ingest one ``run_search()`` results directory as ``run_id``.
    ``params`` (``Search`` column values) fills in the ``search`` row; the
    run's own ``results_dir`` and ``output_format`` are recorded unless
    overridden. Returns a ``ResultsSummary``. Commits."""
    from find_asteroids.results import read_results

    results_dir = Path(results_dir)
    if not results_dir.is_dir():
        raise FileNotFoundError(f"results directory {results_dir} does not exist")
    summary = ResultsSummary()
    todo, summary.skipped_run_ids = _claim_run_ids(session, [run_id], force)
    if not todo:
        session.commit()
        return summary

    search_columns = _model_columns(Search) - {"run_id"}
    search_params = {"results_dir": str(results_dir.resolve()), "output_format": output_format}
    search_params.update({k: v for k, v in (params or {}).items() if k in search_columns})
    search_id = session.execute(
        Search.__table__.insert().returning(Search.id), {"run_id": run_id, **search_params}
    ).scalar_one()

    def rows(name):
        for row in read_results(results_dir, name, output_format=output_format):
            yield {str(k): _clean(v) for k, v in row.items()}

    result_rows = [_split_extra(r, Result) for r in rows("result")]
    new_ids = dict(zip(
        (r["result_num"] for r in result_rows), _insert_results(session, search_id, result_rows)
    ))
    summary.n_results = len(result_rows)
    for name, model in zip(TABLE_NAMES[1:], CHILD_MODELS):
        batch = []
        for row in rows(name):
            row["result_id"] = new_ids[row.pop("result_num")]
            batch.append(_split_extra(row, model))
        _insert_children(session, model, batch)
        setattr(summary, f"n_{'tracklets' if name == 'tracklet' else name}", len(batch))
    summary.run_ids = [run_id]
    _note_unmatched(session, summary)
    session.commit()
    return summary


def source_run_ids(source_engine, run_ids=None, nights=None):
    """The ``Search.run_id``s in a source results database, optionally
    restricted to ``run_ids`` and/or run_ids naming one of ``nights``."""
    with Session(source_engine) as src:
        query = select(Search.run_id).order_by(Search.run_id)
        if run_ids:
            query = query.where(Search.run_id.in_(list(run_ids)))
        found = list(src.scalars(query))
    if nights:
        wanted = set(nights)
        found = [r for r in found if run_id_night(r) in wanted]
    return found


def _source_columns(source_engine, model):
    """The ``model`` columns the source table actually has (it may predate newer ones)."""
    have = {c["name"] for c in inspect(source_engine).get_columns(model.__tablename__)}
    return [c for c in model.__table__.columns if c.name in have]


def ingest_results_db(session, source_engine, run_ids=None, nights=None, force=False):
    """Copy every run (optionally restricted to ``run_ids`` / ``nights``)
    from a source find-asteroids results database, remapping row ids.
    Returns a ``ResultsSummary``. Commits."""
    summary = ResultsSummary()
    wanted = source_run_ids(source_engine, run_ids=run_ids, nights=nights)
    todo, summary.skipped_run_ids = _claim_run_ids(session, wanted, force)

    search_cols = _source_columns(source_engine, Search)
    result_cols = _source_columns(source_engine, Result)
    child_cols = {m: _source_columns(source_engine, m) for m in CHILD_MODELS}

    with Session(source_engine) as src:
        for run_id in todo:
            search_row = src.execute(select(*search_cols).where(Search.run_id == run_id)).mappings().one()
            old_search_id = search_row["id"]
            new_search_id = session.execute(
                Search.__table__.insert().returning(Search.id),
                {k: v for k, v in search_row.items() if k != "id"},
            ).scalar_one()

            old = src.execute(
                select(*result_cols).where(Result.search_id == old_search_id).order_by(Result.id)
            ).mappings().all()
            result_rows = [{k: v for k, v in r.items() if k not in ("id", "search_id")} for r in old]
            new_ids = dict(zip((r["id"] for r in old), _insert_results(session, new_search_id, result_rows)))
            summary.n_results += len(old)

            for model in CHILD_MODELS:
                stream = src.execute(
                    select(*child_cols[model]).where(model.result_id.in_(select(Result.id).where(
                        Result.search_id == old_search_id
                    ))).order_by(model.id).execution_options(yield_per=BATCH_SIZE)
                ).mappings()
                count, batch = 0, []
                for r in stream:
                    batch.append({k: v for k, v in r.items() if k != "id"} | {"result_id": new_ids[r["result_id"]]})
                    if len(batch) >= BATCH_SIZE:
                        _insert_children(session, model, batch)
                        count, batch = count + len(batch), []
                _insert_children(session, model, batch)
                count += len(batch)
                attr = {"gathered": "n_gathered", "points": "n_points", "tracklet": "n_tracklets"}[model.__tablename__]
                setattr(summary, attr, getattr(summary, attr) + count)
            summary.run_ids.append(run_id)
            session.commit()  # one run at a time: bounded memory, and a failure loses only the current run
    _note_unmatched(session, summary)
    session.commit()
    return summary
