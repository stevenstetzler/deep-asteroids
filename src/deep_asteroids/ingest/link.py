"""
Link the pieces of one object's night of MPC observations into one tracklet.

deep-db's submissions sometimes split one real tracklet's observations across
several ``trkid``\\ s, for undesignated and designated objects alike. Each is
fit on its own by ``ingest tracklets mpc-observations`` (an ``mpc_observation``
row), and a short fragment is a poor tracklet when extrapolated across a
whole night, so a result matching the object can miss every fragment. This
groups the fragments and refits each group over the union of its
observations; the groups are ``known_tracklet`` rows with source
``mpc_linked``, the ones crossmatching uses for submitted observations.
Salad's ``super_tracklet`` (``mpc_link.py``) is the same idea.

Per night:

1. Take the night's fitted ``mpc_observation`` rows (``flag`` NULL or
   ``large_residual``). Salad excluded the same tracklets before linking:
   conflated ones (``conflated_trkid``: observations attributed to more than
   one object), ones with two same-time observations more than 5" apart
   (``same_obstime_conflict``), and ones that cannot be fit
   (``too_few_points``); those flags are already on the ``mpc_observation``
   rows, so nothing is excluded twice and they get no linked row.
2. Predict every fit at the night's exposure midpoints (all of deep-db's
   exposures that night, in TAI) and cluster the tracklets whose predictions
   agree within ``match_radius_arcsec`` *on average over those times*, by
   union-find (``crossmatch.geometry.cluster_results``, the same test
   ``dedup`` uses). Agreement at one instant is not enough: two unrelated
   objects whose tracks cross would merge, which salad's history records
   happening once.
3. A group of one keeps its fit; a larger group is refit over its members'
   observations, re-read from deep-db, with the same least-squares fit as the
   per-``trkid`` rows.
4. Each group's statuses are the union of its members'. Its designations are
   the distinct designations among them; two different ones (two known
   objects on the same track) are reported in ``n_designations``, not split.

Salad's clustering used HEALPix buckets and a robust (randomized) fit; this
uses the exact KD-tree pair search and the repo's deterministic fit, so the
results are repeatable. Idempotent per night: a night that already has linked
rows is skipped, unless ``force``.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import delete, insert, select, text

from deep_asteroids.crossmatch.geometry import cluster_results, exposure_times_tai, positions
from deep_asteroids.database.crossmatch import invalidate_crossmatch
from deep_asteroids.database.tracklets import KnownTracklet, KnownTrackletMember
from deep_asteroids.ingest.fit import fit_linear_tracklets, utc_to_tai
from deep_asteroids.ingest.tracklets import (
    MAX_RESIDUAL_ARCSEC,
    MPC_OBSTIME_MJD,
    MPC_POINTS,
    _bind_nights,
    _fetch_array,
    _nights_clause,
)

DEFAULT_MATCH_RADIUS_ARCSEC = 1.0  # salad's: MPC fits are far more accurate than a search result's

NIGHT_EXPOSURES = """
SELECT DISTINCT e.midpoint_mjd FROM exposure e JOIN night n ON n.id = e.night_id WHERE n.night = :night
"""


@dataclass
class LinkSummary:
    nights: list = field(default_factory=list)
    skipped_nights: list = field(default_factory=list)
    n_members: int = 0  # fitted mpc_observation tracklets that were linked
    n_unlinked: int = 0  # mpc_observation tracklets without a fit (flagged), left out
    n_groups: int = 0
    group_sizes: dict = field(default_factory=dict)  # size -> number of groups
    n_conflicting_designations: int = 0
    n_flagged_large_residual: int = 0
    n_crossmatch_runs_invalidated: int = 0

    @property
    def n_merged_away(self):
        return self.n_members - self.n_groups

    def format(self):
        lines = [
            f"link: {len(self.nights)} night(s) linked, {len(self.skipped_nights)} skipped "
            "(already linked; --force to redo)"
        ]
        if self.nights:
            sizes = ", ".join(f"{n}x size {s}" for s, n in sorted(self.group_sizes.items()))
            lines.append(
                f"  {self.n_members:,} fitted MPC tracklet(s) -> {self.n_groups:,} group(s); "
                f"{self.n_merged_away:,} merged into another ({sizes})"
            )
            lines.append(f"  {self.n_unlinked:,} flagged tracklet(s) without a fit were left out")
            if self.n_conflicting_designations:
                lines.append(f"  {self.n_conflicting_designations:,} group(s) join more than one designation")
            if self.n_flagged_large_residual:
                lines.append(f"  {self.n_flagged_large_residual:,} refit group(s) flagged large_residual")
            if self.n_crossmatch_runs_invalidated:
                lines.append(
                    f"  {self.n_crossmatch_runs_invalidated:,} stored cross-match run(s) for these nights were "
                    "invalidated: run `crossmatch` again")
        return "\n".join(lines)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _night_points(deep_engine, nights):
    """``{night: (tracklet_id, t_tai, ra, dec)}`` of every MPC observation on the given nights."""
    dialect = deep_engine.dialect.name
    if dialect not in MPC_OBSTIME_MJD:
        raise NotImplementedError(f"deep-db on {dialect!r} is not supported (obstime -> MJD)")
    points = _fetch_array(
        deep_engine, _bind_nights(MPC_POINTS.format(mjd=MPC_OBSTIME_MJD[dialect], nights=_nights_clause(nights)),
                                  nights),
        {"nights": list(nights)}, ncols=6,
    )
    out = {}
    for night in nights:
        p = points[points[:, 1] == night] if len(points) else points
        out[night] = (p[:, 0].astype(np.int64), utc_to_tai(p[:, 2]), p[:, 3], p[:, 4]) if len(p) else None
    return out


def delete_linked(session, nights):
    """Delete the ``mpc_linked`` rows (and their membership) of ``nights``. Returns how many."""
    ids = session.scalars(select(KnownTracklet.id).where(
        KnownTracklet.source == "mpc_linked", KnownTracklet.night.in_(list(nights)))).all()
    if ids:
        session.execute(delete(KnownTrackletMember).where(KnownTrackletMember.linked_id.in_(ids)))
        session.execute(delete(KnownTracklet).where(KnownTracklet.id.in_(ids)))
    return len(ids)


def _night_members(session, night):
    """The night's fitted per-trkid rows as plain tuples (a commit would expire ORM objects)."""
    return session.execute(
        select(KnownTracklet.id, KnownTracklet.deep_db_mpc_tracklet_id, KnownTracklet.provid,
               KnownTracklet.permid, KnownTracklet.name, KnownTracklet.statuses, KnownTracklet.epoch,
               KnownTracklet.ra_epoch, KnownTracklet.dec_epoch, KnownTracklet.vra, KnownTracklet.vdec,
               KnownTracklet.t_min, KnownTracklet.t_max, KnownTracklet.n_points,
               KnownTracklet.max_residual_arcsec, KnownTracklet.flag)
        .where(KnownTracklet.source == "mpc_observation", KnownTracklet.night == night,
               KnownTracklet.epoch.is_not(None)).order_by(KnownTracklet.id)).all()


def link_mpc_tracklets(session, deep_engine, nights, match_radius_arcsec=DEFAULT_MATCH_RADIUS_ARCSEC,
                       max_residual_arcsec=MAX_RESIDUAL_ARCSEC, force=False, deep_db_sha=None):
    """Link the fitted ``mpc_observation`` tracklets of ``nights`` (those already
    ingested; ``ingest tracklets mpc-observations`` first) into ``mpc_linked``
    ones. Returns a ``LinkSummary``. Commits."""
    summary = LinkSummary()
    nights = sorted(set(nights))
    existing = set(session.scalars(select(KnownTracklet.night).where(
        KnownTracklet.source == "mpc_linked", KnownTracklet.night.in_(nights)).distinct()))
    todo = nights if force else [n for n in nights if n not in existing]
    summary.skipped_nights = [] if force else [n for n in nights if n in existing]
    todo = [n for n in todo if session.scalar(select(KnownTracklet.id).where(
        KnownTracklet.source == "mpc_observation", KnownTracklet.night == n).limit(1)) is not None]
    if not todo:
        session.commit()
        return summary
    delete_linked(session, todo)
    summary.n_crossmatch_runs_invalidated = invalidate_crossmatch(session, todo)
    points = _night_points(deep_engine, todo)

    for night in todo:
        members = _night_members(session, night)
        summary.n_unlinked += session.scalar(
            select(text("count(*)")).select_from(KnownTracklet).where(
                KnownTracklet.source == "mpc_observation", KnownTracklet.night == night,
                KnownTracklet.epoch.is_(None)))
        if not members:
            continue
        with deep_engine.connect() as conn:
            midpoints = [r[0] for r in conn.execute(text(NIGHT_EXPOSURES), {"night": night})]
        times = exposure_times_tai(midpoints)

        epoch, ra_epoch, dec_epoch, vra, vdec = (np.array([m[i] for m in members], dtype=float) for i in range(6, 11))
        ra, dec = positions(ra_epoch - vra * epoch, dec_epoch - vdec * epoch, vra, vdec, times)
        labels = cluster_results(ra, dec, match_radius_arcsec)

        night_pts = points[night]
        rows, member_ids, created_at = [], [], _now()
        for label in np.unique(labels):
            idx = np.nonzero(labels == label)[0]
            group = [members[i] for i in idx]
            statuses = sorted({s for m in group for s in (m[5] or "").split(",") if s})
            designations = sorted({m[2] for m in group if m[2]})
            if len(group) == 1:
                m = group[0]
                fit = {"epoch": m[6], "ra_epoch": m[7], "dec_epoch": m[8], "vra": m[9], "vdec": m[10],
                       "t_min": m[11], "t_max": m[12], "n_points": m[13], "max_residual_arcsec": m[14],
                       "flag": m[15]}
            else:
                wanted = np.isin(night_pts[0], [m[1] for m in group])
                f = fit_linear_tracklets(np.zeros(int(wanted.sum()), dtype=int), night_pts[1][wanted],
                                         night_pts[2][wanted], night_pts[3][wanted])
                if not np.isfinite(f["vra"][0]):  # every member has a fit, so this means a degenerate union
                    continue
                flag = "large_residual" if f["max_residual_arcsec"][0] > max_residual_arcsec else None
                summary.n_flagged_large_residual += flag is not None
                fit = {"epoch": float(f["epoch"][0]), "ra_epoch": float(f["ra_epoch"][0]),
                       "dec_epoch": float(f["dec_epoch"][0]), "vra": float(f["vra"][0]),
                       "vdec": float(f["vdec"][0]), "t_min": float(f["t_min"][0]), "t_max": float(f["t_max"][0]),
                       "n_points": int(f["n"][0]), "max_residual_arcsec": float(f["max_residual_arcsec"][0]),
                       "flag": flag}
            named = next((m for m in group if m[2]), None) if len(designations) == 1 else None
            rows.append({
                "source": "mpc_linked", "night": night, **fit, "statuses": ",".join(statuses),
                "n_members": len(group), "n_designations": len(designations),
                "designations": ",".join(designations) or None,
                "provid": named[2] if named else None, "permid": named[3] if named else None,
                "name": named[4] if named else None, "deep_db_sha": deep_db_sha, "created_at": created_at,
            })
            member_ids.append([m[0] for m in group])
            summary.group_sizes[len(group)] = summary.group_sizes.get(len(group), 0) + 1
            summary.n_conflicting_designations += len(designations) > 1

        if rows:
            linked_ids = session.scalars(
                insert(KnownTracklet).returning(KnownTracklet.id, sort_by_parameter_order=True), rows).all()
            session.execute(insert(KnownTrackletMember), [
                {"linked_id": linked, "member_id": member}
                for linked, ids in zip(linked_ids, member_ids, strict=True) for member in ids])
        summary.nights.append(night)
        summary.n_members += sum(len(ids) for ids in member_ids)
        summary.n_groups += len(rows)
    session.commit()
    return summary
