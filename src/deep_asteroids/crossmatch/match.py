"""
Cross-match find-asteroids results against known objects, within a night.

Each result's tracklet is compared with every ``known_tracklet`` on the same
night that has a fit -- submitted MPC observations (``mpc_observation``),
mpchecker ephemerides (``mpchecker``) and synthetic objects (``synthetic``) --
by evaluating both at the search's exposure times (deep-db's UTC midpoints in
TAI) and taking the mean great-circle separation. Every pair below
``max_sep_arcsec`` is stored in ``crossmatch``; "the closest match of each
source" is a query over it.

Salad's crossmatch refit the known objects' points per run through a DuckDB
bridge to deep-db. Here ``known_tracklet`` is already fit and lives in the
same database, so this is a join, with a KD-tree prefilter
(``geometry.pairs_within``) instead of all pairs.

Idempotent per search run: a run in ``crossmatch_run`` is skipped unless
``force``.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from find_asteroids.models import Result, Search, Tracklet
from sqlalchemy import delete, func, insert, select

from deep_asteroids.crossmatch.geometry import exposure_times_tai, pair_separations, pairs_within, positions
from deep_asteroids.database.crossmatch import CrossmatchCandidate, CrossmatchRun, ResultDedup
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure
from deep_asteroids.database.tracklets import SOURCES, KnownTracklet

DEFAULT_MAX_SEP_ARCSEC = 30.0
# a result counts as a match of its source when the closest candidate is below this mean separation
DEFAULT_MATCH_RADII = {"mpc_observation": 2.0, "synthetic": 2.0, "mpchecker": 10.0}
LABELS = {"mpc_observation": "submitted", "mpchecker": "known", "synthetic": "synthetic"}


@dataclass
class CrossmatchSummary:
    runs: list = field(default_factory=list)
    skipped_runs: list = field(default_factory=list)
    n_results: int = 0
    n_candidates: int = 0

    def format(self, counts=None, radii=DEFAULT_MATCH_RADII):
        lines = [
            f"crossmatch: {len(self.runs)} run(s) matched, {len(self.skipped_runs)} skipped "
            "(already done; --force to redo)"
        ]
        if self.runs:
            lines.append(f"  {self.n_results:,} result(s), {self.n_candidates:,} candidate pair(s) stored")
        if counts:
            lines.append(match_table(counts, radii))
        return "\n".join(lines)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _runs(session, nights, run_ids):
    """``[(run_id, search_definition_id, night)]`` of searches that have results."""
    q = (
        select(Search.run_id, SearchDefinition.id, SearchDefinition.night)
        .join(SearchDefinition, SearchDefinition.run_id == Search.run_id)
        .where(Search.id.in_(select(Result.search_id)))
    )
    if nights:
        q = q.where(SearchDefinition.night.in_(list(nights)))
    if run_ids:
        q = q.where(Search.run_id.in_(list(run_ids)))
    return session.execute(q.order_by(SearchDefinition.night, Search.run_id)).all()


@dataclass
class Known:
    """A night's fitted known tracklets as plain arrays (not ORM objects, which a commit expires and
    reloads one row at a time)."""
    ids: np.ndarray
    sources: np.ndarray
    ra_0: np.ndarray
    dec_0: np.ndarray
    vra: np.ndarray
    vdec: np.ndarray

    def __len__(self):
        return len(self.ids)


def _known(session, night, sources):
    """The night's known tracklets that have a fit, from ``sources``."""
    rows = session.execute(
        select(KnownTracklet.id, KnownTracklet.source, KnownTracklet.epoch, KnownTracklet.ra_epoch,
               KnownTracklet.dec_epoch, KnownTracklet.vra, KnownTracklet.vdec)
        .where(KnownTracklet.night == night, KnownTracklet.source.in_(list(sources)),
               KnownTracklet.epoch.is_not(None)).order_by(KnownTracklet.id)).all()
    epoch, ra_epoch, dec_epoch, vra, vdec = (np.array([r[i] for r in rows], dtype=float) for i in range(2, 7))
    # to find-asteroids' MJD-0 convention (KnownTracklet.ra_0/dec_0)
    return Known(
        ids=np.array([r[0] for r in rows], dtype=np.int64), sources=np.array([r[1] for r in rows], dtype=object),
        ra_0=ra_epoch - vra * epoch, dec_0=dec_epoch - vdec * epoch, vra=vra, vdec=vdec)


def crossmatch_results(session, nights=None, run_ids=None, sources=SOURCES,
                       max_sep_arcsec=DEFAULT_MAX_SEP_ARCSEC, force=False):
    """Cross-match the results of the selected search runs (default: every
    run with results) against known tracklets of ``sources`` on the same
    night. Returns a ``CrossmatchSummary``. Commits."""
    summary = CrossmatchSummary()
    known_by_night = {}
    for run_id, definition_id, night in _runs(session, nights, run_ids):
        done = session.scalar(select(CrossmatchRun.id).where(CrossmatchRun.run_id == run_id))
        if done is not None and not force:
            summary.skipped_runs.append(run_id)
            continue

        result_ids = session.scalars(select(Result.id).join(Search, Search.id == Result.search_id)
                                     .where(Search.run_id == run_id)).all()
        if done is not None:
            session.execute(delete(CrossmatchCandidate).where(CrossmatchCandidate.result_id.in_(result_ids)))
            session.execute(delete(CrossmatchRun).where(CrossmatchRun.run_id == run_id))

        if night not in known_by_night:
            known_by_night[night] = _known(session, night, sources)
        known = known_by_night[night]

        midpoints = session.scalars(select(SearchDefinitionExposure.midpoint_mjd).where(
            SearchDefinitionExposure.search_definition_id == definition_id).distinct()).all()
        times = exposure_times_tai(midpoints)
        res = session.execute(
            select(Result.id, Tracklet.vra, Tracklet.vdec, Tracklet.ra_0, Tracklet.dec_0)
            .join(Tracklet, Tracklet.result_id == Result.id).join(Search, Search.id == Result.search_id)
            .where(Search.run_id == run_id).order_by(Result.id)).all()

        created_at = _now()
        n_candidates = 0
        if res and known:
            r_ids = np.array([r[0] for r in res])
            r_vra, r_vdec = np.array([r[1] for r in res]), np.array([r[2] for r in res])
            r_ra, r_dec = positions([r[3] for r in res], [r[4] for r in res], r_vra, r_vdec, times)
            k_vra, k_vdec = known.vra, known.vdec
            k_ra, k_dec = positions(known.ra_0, known.dec_0, k_vra, k_vdec, times)

            pairs = pairs_within(r_ra, r_dec, max_sep_arcsec, k_ra, k_dec)
            if len(pairs):
                sep = pair_separations(r_ra, r_dec, k_ra, k_dec, pairs)  # (pairs, times)
                mean = sep.mean(axis=1)
                keep = mean < max_sep_arcsec
                pairs, sep, mean = pairs[keep], sep[keep], mean[keep]
                cos_dec = np.cos(np.radians(r_dec[pairs[:, 0]].mean(axis=1)))
                dv = np.hypot((r_vra[pairs[:, 0]] - k_vra[pairs[:, 1]]) * cos_dec,
                              r_vdec[pairs[:, 0]] - k_vdec[pairs[:, 1]])
                rows = [{
                    "result_id": int(r_ids[i]), "known_tracklet_id": int(known.ids[j]), "night": night,
                    "source": str(known.sources[j]), "n_times": len(times), "mean_sep_arcsec": float(m),
                    "min_sep_arcsec": float(s.min()), "max_sep_arcsec": float(s.max()),
                    "total_sep_arcsec": float(s.sum()), "velocity_diff": float(v), "created_at": created_at,
                } for (i, j), m, s, v in zip(pairs, mean, sep, dv)]
                if rows:
                    session.execute(insert(CrossmatchCandidate), rows)
                n_candidates = len(rows)

        session.add(CrossmatchRun(
            run_id=run_id, night=night, sources=",".join(sorted(sources)), max_sep_arcsec=max_sep_arcsec,
            n_results=len(res), n_known_tracklets=len(known), n_candidates=n_candidates, created_at=created_at))
        session.commit()
        summary.runs.append(run_id)
        summary.n_results += len(res)
        summary.n_candidates += n_candidates
    return summary


def match_counts(session, run_ids, radii=DEFAULT_MATCH_RADII):
    """Per source: how many of the runs' results have their closest candidate within ``radii[source]``,
    and how many of the distinct (authoritative) objects do. Returns ``{"n_results", "n_objects",
    source: (n_results_matched, n_objects_matched)}``. A result with no ``result_dedup`` row (never
    de-duplicated) counts as its own object."""
    result_ids = session.scalars(select(Result.id).join(Search, Search.id == Result.search_id)
                                 .where(Search.run_id.in_(list(run_ids)))).all()
    dedup = dict(session.execute(select(ResultDedup.result_id, ResultDedup.is_authoritative)
                                 .where(ResultDedup.result_id.in_(result_ids))).all())
    authoritative = {i for i in result_ids if dedup.get(i, True)}
    out = {"n_results": len(result_ids), "n_objects": len(authoritative)}
    best = session.execute(
        select(CrossmatchCandidate.source, CrossmatchCandidate.result_id, func.min(CrossmatchCandidate.mean_sep_arcsec))
        .where(CrossmatchCandidate.result_id.in_(result_ids))
        .group_by(CrossmatchCandidate.source, CrossmatchCandidate.result_id)).all()
    for source in SOURCES:
        hits = {r for s, r, m in best if s == source and m < radii[source]}
        out[source] = (len(hits), len(hits & authoritative))
    return out


def match_table(counts, radii=DEFAULT_MATCH_RADII):
    lines = [f"  {'':<16} {'results':>8} {'objects':>8}",
             f"  {'total':<16} {counts['n_results']:>8,} {counts['n_objects']:>8,}"]
    for source in SOURCES:
        n, objects = counts[source]
        lines.append(f"  {LABELS[source] + ' < ' + format(radii[source], 'g') + chr(34):<16} {n:>8,} {objects:>8,}")
    return "\n".join(lines)
