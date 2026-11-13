"""
De-duplication of find-asteroids results and their cross-match against known
objects. See ``crossmatch/dedup.py`` and ``crossmatch/match.py``.

Both tables refer to find-asteroids' ``result`` (and, for matches, our
``known_tracklet``) by real foreign keys: everything lives in one database.
"""
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    delete,
)

from deep_asteroids.database.models import Base


class ResultDedup(Base):
    """One row per result in a ``(night, field)`` pointing group once it has
    been de-duplicated: which cluster of redetections of the same object it
    belongs to, and which member of that cluster is authoritative.

    A shift-and-stack search commonly finds the same moving object in several
    adjacent search cells (and on several CCDs of a pointing group), each a
    separate ``result``/``tracklet``. Two results are redetections when their
    tracklets agree, on average over the group's exposure times, to within
    ``merge_radius_arcsec``; a cluster is the transitive closure of that
    (union-find). A result that merged with nothing is its own, authoritative,
    cluster of one. Nothing is deleted or rewritten: counting "distinct
    objects" means counting ``is_authoritative`` rows.
    """

    __tablename__ = "result_dedup"
    __table_args__ = (
        UniqueConstraint("result_id", name="uq_result_dedup_result"),
        Index("ix_result_dedup_night_field", "night", "field"),
    )

    id = Column(Integer, primary_key=True)
    result_id = Column(Integer, ForeignKey("result.id"), nullable=False)
    night = Column(Integer, nullable=False)
    field = Column(String, nullable=False)
    authoritative_result_id = Column(Integer, ForeignKey("result.id"), nullable=False, index=True)
    is_authoritative = Column(Boolean, nullable=False)
    n_members = Column(Integer, nullable=False)  # size of this result's cluster
    merge_radius_arcsec = Column(Float, nullable=False)
    created_at = Column(DateTime, nullable=False)


class CrossmatchRun(Base):
    """One row per search run crossmatched, so a rerun can skip it (a result
    with no candidate leaves no ``crossmatch`` row to say it was processed)."""

    __tablename__ = "crossmatch_run"

    id = Column(Integer, primary_key=True)
    run_id = Column(String, nullable=False, unique=True)
    night = Column(Integer, nullable=False, index=True)
    sources = Column(String, nullable=False)  # the known_tracklet sources matched against, comma-joined
    max_sep_arcsec = Column(Float, nullable=False)  # a candidate is stored only below this mean separation
    n_results = Column(Integer, nullable=False)
    n_known_tracklets = Column(Integer, nullable=False)
    n_candidates = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False)


class CrossmatchCandidate(Base):
    """One row per ``(result, known_tracklet)`` pair whose tracklets agree,
    on average over the search's exposure times, to within the run's
    ``max_sep_arcsec``. All candidates are kept, not only the closest: the
    same result often matches both a submitted tracklet and an ephemeris, and
    "the closest per source" is a query over this table.

    Both tracklets are evaluated at the search's exposure midpoints (deep-db's
    UTC midpoints, in TAI); separations are great-circle, in arcsec.
    ``velocity_diff`` is the size of the difference of the two velocities, in
    deg/day on the sky (the RA rate scaled by cos(dec)). ``source`` repeats
    ``known_tracklet.source``, for filtering without a join.
    """

    __tablename__ = "crossmatch"
    __table_args__ = (
        UniqueConstraint("result_id", "known_tracklet_id", name="uq_crossmatch_result_known"),
        Index("ix_crossmatch_source_mean_sep", "source", "mean_sep_arcsec"),
    )

    id = Column(Integer, primary_key=True)
    result_id = Column(Integer, ForeignKey("result.id"), nullable=False, index=True)
    known_tracklet_id = Column(Integer, ForeignKey("known_tracklet.id"), nullable=False, index=True)
    night = Column(Integer, nullable=False, index=True)
    source = Column(String, nullable=False)
    n_times = Column(Integer, nullable=False)
    mean_sep_arcsec = Column(Float, nullable=False)
    min_sep_arcsec = Column(Float, nullable=False)
    max_sep_arcsec = Column(Float, nullable=False)
    total_sep_arcsec = Column(Float, nullable=False)
    velocity_diff = Column(Float, nullable=False)
    created_at = Column(DateTime, nullable=False)


def invalidate_crossmatch(session, nights):
    """Delete the stored cross-matches of ``nights`` (candidates and the
    per-run markers), because the night's known tracklets have changed and
    the next ``crossmatch`` must recompute them. Returns how many runs were invalidated."""
    nights = list(nights)
    runs = session.execute(delete(CrossmatchRun).where(CrossmatchRun.night.in_(nights))).rowcount
    session.execute(delete(CrossmatchCandidate).where(CrossmatchCandidate.night.in_(nights)))
    return runs
