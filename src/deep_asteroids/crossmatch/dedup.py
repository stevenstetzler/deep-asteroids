"""
De-duplicate find-asteroids results within a night.

A shift-and-stack search commonly finds one real moving object in several
adjacent search cells, and on several CCDs of a pointing group: each becomes
its own ``result``/``tracklet`` with no relationship recorded. This groups
those redetections.

Works per ``(night, field)`` pointing group (every search of every CCD of
that field on that night): two results are redetections when their tracklets
agree, on average over the group's exposure times (the distinct deep-db
midpoints of its search definitions, in TAI), to within
``merge_radius_arcsec``; a cluster is the transitive closure of that, by
union-find. Pairs are found without comparing all of them: a pair whose mean
separation is small is small at one of the times, so only pairs near each
other at some exposure are tested (``geometry.pairs_within``). This is the
core of salad's ``dedup.py``/``result_merge.py``, with the grid taken from
``search_definition_exposure`` instead of a database round trip.

Each cluster has one authoritative member, the one with the most
Hough-stage points (``result.n``), the lowest result id breaking ties. Unlike
salad, nothing is refit from the cluster's union of points (that refit draws
from an unseeded RNG, see the README) and nothing is created: ``result_dedup``
rows record the clusters, and counting distinct objects is counting
``is_authoritative`` rows.

Idempotent per pointing group: a group whose results all have a
``result_dedup`` row is skipped, unless ``force``.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from find_asteroids.models import Result, Search, Tracklet
from sqlalchemy import delete, func, insert, select

from deep_asteroids.crossmatch.geometry import cluster_results, exposure_times_tai, positions
from deep_asteroids.database.crossmatch import ResultDedup
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure

DEFAULT_MERGE_RADIUS_ARCSEC = 2.0


@dataclass
class DedupSummary:
    groups: list = field(default_factory=list)  # (night, field) processed
    skipped_groups: list = field(default_factory=list)  # (night, field) already done
    n_results: int = 0
    n_clusters: int = 0
    n_unassigned: int = 0  # results whose run_id has no search definition, so no night/field to group by
    cluster_sizes: dict = field(default_factory=dict)  # size -> number of clusters (processed groups)

    @property
    def n_merged_away(self):
        return self.n_results - self.n_clusters

    def format(self):
        lines = [
            f"dedup: {len(self.groups)} pointing group(s) processed, {len(self.skipped_groups)} skipped "
            "(already done; --force to redo)"
        ]
        if self.groups:
            sizes = ", ".join(f"{n}x size {s}" for s, n in sorted(self.cluster_sizes.items()))
            lines.append(
                f"  {self.n_results:,} result(s) -> {self.n_clusters:,} distinct object(s); "
                f"{self.n_merged_away:,} redetection(s) merged away ({sizes})"
            )
        if self.n_unassigned:
            lines.append(f"  {self.n_unassigned:,} result(s) belong to a run_id with no search definition: not grouped")
        return "\n".join(lines)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _group_rows(session, nights, fields, run_ids):
    """Every selected result with its tracklet and pointing group:
    ``(result_id, n, vra, vdec, ra_0, dec_0, night, field, search_definition_id)``."""
    q = (
        select(Result.id, Result.n, Tracklet.vra, Tracklet.vdec, Tracklet.ra_0, Tracklet.dec_0,
               SearchDefinition.night, SearchDefinition.field, SearchDefinition.id)
        .join(Search, Search.id == Result.search_id)
        .join(Tracklet, Tracklet.result_id == Result.id)
        .join(SearchDefinition, SearchDefinition.run_id == Search.run_id)
    )
    if nights:
        q = q.where(SearchDefinition.night.in_(list(nights)))
    if fields:
        q = q.where(SearchDefinition.field.in_(list(fields)))
    if run_ids:
        q = q.where(Search.run_id.in_(list(run_ids)))
    return session.execute(q.order_by(Result.id)).all()


def dedup_results(session, nights=None, fields=None, run_ids=None,
                  merge_radius_arcsec=DEFAULT_MERGE_RADIUS_ARCSEC, force=False):
    """De-duplicate the selected results (default: all) per ``(night, field)``
    pointing group. Returns a ``DedupSummary``. Commits."""
    summary = DedupSummary()
    rows = _group_rows(session, nights, fields, run_ids)

    # results with no search definition cannot be placed in a group
    q = select(func.count()).select_from(Result).join(Search, Search.id == Result.search_id).where(
        ~Search.run_id.in_(select(SearchDefinition.run_id)))
    if run_ids:
        q = q.where(Search.run_id.in_(list(run_ids)))
    summary.n_unassigned = session.scalar(q)

    groups = {}
    for row in rows:
        groups.setdefault((row[6], row[7]), []).append(row)

    for (night, field_), members in sorted(groups.items()):
        ids = [m[0] for m in members]
        done = session.scalar(select(func.count()).select_from(ResultDedup).where(ResultDedup.result_id.in_(ids)))
        if done == len(ids) and not force:
            summary.skipped_groups.append((night, field_))
            continue
        session.execute(delete(ResultDedup).where(ResultDedup.result_id.in_(ids)))

        definition_ids = sorted({m[8] for m in members})
        midpoints = session.scalars(
            select(SearchDefinitionExposure.midpoint_mjd).where(
                SearchDefinitionExposure.search_definition_id.in_(definition_ids)).distinct()).all()
        times = exposure_times_tai(midpoints)
        n_points = np.array([m[1] for m in members])
        ra, dec = positions([m[4] for m in members], [m[5] for m in members],
                            [m[2] for m in members], [m[3] for m in members], times)
        labels = cluster_results(ra, dec, merge_radius_arcsec)

        created_at = _now()
        out = []
        for label in np.unique(labels):
            idx = np.nonzero(labels == label)[0]
            # most Hough points, then the lowest result id
            best = min(idx, key=lambda i: (-n_points[i], ids[i]))
            summary.cluster_sizes[len(idx)] = summary.cluster_sizes.get(len(idx), 0) + 1
            for i in idx:
                out.append({
                    "result_id": ids[i], "night": night, "field": field_,
                    "authoritative_result_id": ids[best], "is_authoritative": bool(i == best),
                    "n_members": len(idx), "merge_radius_arcsec": merge_radius_arcsec, "created_at": created_at,
                })
        session.execute(insert(ResultDedup), out)
        summary.groups.append((night, field_))
        summary.n_results += len(ids)
        summary.n_clusters += len(np.unique(labels))
    session.commit()
    return summary
