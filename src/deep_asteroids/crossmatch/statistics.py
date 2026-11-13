"""
Per-result summary statistics, computed in one vectorized pass per search run.

For every result: the median residual of its gathered points against its own
tracklet (the main separator of real moving objects from chance alignments),
how many points are outliers, its speed and velocity uncertainty, and the
median magnitude of its points. Ported from salad's ``result_stats.py``,
extended with ``speed``, ``sigma_v``, ``n_times`` and ``median_mag`` (salad
needed them in separate queries), and computed per run from one query of the
run's gathered points instead of one query per result. Needs no cross-match
and no deep-db.

Idempotent per run: a run whose results all have a row is skipped, unless
``force``.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from find_asteroids.models import Gathered, Result, Search, Tracklet
from sqlalchemy import delete, func, insert, select

from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.database.statistics import ResultStatistics
from deep_asteroids.ingest.fit import sep_arcsec
from deep_asteroids.ingest.tracklets import _group_median

# a gathered point further than this from its result's tracklet counts as an outlier (salad's, and ADES's)
MAX_RESIDUAL_ARCSEC = 1.0


@dataclass
class StatisticsSummary:
    runs: list = field(default_factory=list)
    skipped_runs: list = field(default_factory=list)
    n_results: int = 0
    n_empty: int = 0  # results with no gathered points

    def format(self):
        lines = [f"statistics: {len(self.runs)} run(s) computed, {len(self.skipped_runs)} skipped "
                 "(already done; --force to redo)"]
        if self.runs:
            lines.append(f"  {self.n_results:,} result(s)" + (f", {self.n_empty:,} with no gathered points"
                                                             if self.n_empty else ""))
        return "\n".join(lines)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def result_statistics_rows(result_ids, tracklets, gathered):
    """The ``result_statistics`` rows (dicts) for ``result_ids``, given ``tracklets``
    (``{result_id: (vra, vdec, ra_0, dec_0, sigma_vra, sigma_vdec)}``) and the ``gathered``
    points as arrays ``(result_id, ra, dec, time, mag)``."""
    g_result, g_ra, g_dec, g_time, g_mag = gathered
    ids = np.asarray(sorted(result_ids))
    t = np.array([tracklets[i] for i in ids], dtype=float).reshape(len(ids), 6)
    idx = np.searchsorted(ids, np.asarray(g_result, dtype=np.int64))  # each point's row in ``ids``

    # residual of each point against its own result's tracklet at the point's time
    if len(g_result):
        ra_pred = (t[idx, 2] + t[idx, 0] * g_time) % 360.0
        dec_pred = t[idx, 3] + t[idx, 1] * g_time
        residual = sep_arcsec(g_ra, g_dec, ra_pred, dec_pred)
    else:
        residual = np.empty(0)

    labels = np.arange(len(ids))
    median_residual = _group_median(idx, residual, labels) if len(idx) else np.full(len(ids), np.nan)
    median_mag = _group_median(idx, g_mag, labels) if len(idx) else np.full(len(ids), np.nan)
    n_gathered = np.bincount(idx, minlength=len(ids))
    n_outliers = np.bincount(idx, weights=(residual > MAX_RESIDUAL_ARCSEC).astype(float), minlength=len(ids))
    # distinct times per result
    pair = np.unique(np.c_[idx, g_time], axis=0) if len(idx) else np.empty((0, 2))
    n_times = np.bincount(pair[:, 0].astype(np.int64), minlength=len(ids)) if len(pair) else np.zeros(len(ids), int)

    out = []
    for k, rid in enumerate(ids):
        mr, mm = median_residual[k], median_mag[k]
        sv = float(np.hypot(t[k, 4], t[k, 5]))
        out.append({
            "result_id": int(rid), "n_gathered": int(n_gathered[k]), "n_times": int(n_times[k]),
            "median_residual_arcsec": None if np.isnan(mr) else float(mr),
            "n_residual_outliers": int(n_outliers[k]), "speed": float(np.hypot(t[k, 0], t[k, 1])),
            "sigma_v": None if np.isnan(sv) else sv, "median_mag": None if np.isnan(mm) else float(mm),
        })
    return out


def compute_result_statistics(session, nights=None, run_ids=None, force=False):
    """Compute ``result_statistics`` for the selected search runs (default: every run
    with results). Returns a ``StatisticsSummary``. Commits."""
    summary = StatisticsSummary()
    q = select(Search.run_id).where(Search.id.in_(select(Result.search_id)))
    if run_ids:
        q = q.where(Search.run_id.in_(list(run_ids)))
    if nights:
        q = q.where(Search.run_id.in_(select(SearchDefinition.run_id).where(SearchDefinition.night.in_(list(nights)))))
    for run_id in session.scalars(q.order_by(Search.run_id)).all():
        result_ids = session.scalars(select(Result.id).join(Search, Search.id == Result.search_id)
                                     .where(Search.run_id == run_id).order_by(Result.id)).all()
        done = session.scalar(select(func.count()).select_from(ResultStatistics)
                              .where(ResultStatistics.result_id.in_(result_ids)))
        if done == len(result_ids) and not force:
            summary.skipped_runs.append(run_id)
            continue
        session.execute(delete(ResultStatistics).where(ResultStatistics.result_id.in_(result_ids)))

        tracklets = {r[0]: r[1:] for r in session.execute(
            select(Tracklet.result_id, Tracklet.vra, Tracklet.vdec, Tracklet.ra_0, Tracklet.dec_0,
                   Tracklet.sigma_vra, Tracklet.sigma_vdec).where(Tracklet.result_id.in_(result_ids)))}
        result_ids = [r for r in result_ids if r in tracklets]  # every result has one; be safe
        points = session.execute(
            select(Gathered.result_id, Gathered.ra, Gathered.dec, Gathered.time, Gathered.mag)
            .where(Gathered.result_id.in_(result_ids))).all()
        arrays = tuple(np.array([p[i] for p in points], dtype=float) if points else np.empty(0) for i in range(5))
        rows = result_statistics_rows(result_ids, {k: tuple(np.nan if x is None else x for x in v)
                                                   for k, v in tracklets.items()}, arrays)
        created_at = _now()
        for r in rows:
            r["computed_at"] = created_at
        if rows:
            session.execute(insert(ResultStatistics), rows)
        session.commit()
        summary.runs.append(run_id)
        summary.n_results += len(rows)
        summary.n_empty += sum(r["n_gathered"] == 0 for r in rows)
    return summary
