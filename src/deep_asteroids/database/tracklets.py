"""
Known-object tracklets: one linear sky-plane fit per object per night,
for every object deep-db knows about. See ``ingest/tracklets.py``.
"""
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)

from deep_asteroids.database.models import Base

SOURCES = ("synthetic", "mpc_observation", "mpc_linked", "mpchecker")
SYNTHETIC_COMPONENTS = ("primary", "companion", "barycenter", "photocenter")
# flags that mean "no fit" (the fit columns are NULL) ...
NO_FIT_FLAGS = ("too_few_points", "conflated_trkid", "same_obstime_conflict")
# ... and flags that keep the fit but mark it suspect
SUSPECT_FIT_FLAGS = ("large_residual",)
FLAGS = NO_FIT_FLAGS + SUSPECT_FIT_FLAGS


def _sql_list(values):
    return ", ".join(f"'{v}'" for v in values)


def _where(condition):
    """A partial-index predicate, for both SQLite and Postgres."""
    return {"sqlite_where": text(condition), "postgresql_where": text(condition)}


class KnownTracklet(Base):
    r"""
    A linear tracklet for one known object on one night:

        ra(t)  = ra_epoch  + vra  * (t - epoch)
        dec(t) = dec_epoch + vdec * (t - epoch)

    valid over ``[t_min, t_max]``. Angles in degrees, rates in deg/day
    (``vra`` is dRA/dt, no cos(dec) factor -- find-asteroids' convention),
    times in MJD **TAI** (find-asteroids' convention; deep-db's UTC times are
    converted on ingest). ``epoch`` is the mean time of the fitted points.

    The fit columns are NULL when there is no fit, with ``flag`` saying why
    (``too_few_points``/``conflated_trkid``/``same_obstime_conflict``) --
    every deep-db object/tracklet on a night gets a row, so none is dropped
    silently. ``flag = 'large_residual'`` keeps the fit but marks it
    suspect: its worst point is further from the line than the ingest's
    threshold (mixed-up observations, or real curvature over a long arc).

    Identity columns refer to deep-db rows by id (no foreign keys: deep-db is
    a separate database). Points are not stored here; re-read them from
    deep-db by these ids. Which identity columns are set depends on
    ``source``:

    * ``synthetic``: ``synthetic_source``/``orbit_id``/``component``;
      ``deep_db_synthetic_object_id`` for ``primary``/``companion`` (the two
      derived binary positions, ``barycenter``/``photocenter``, have none).
    * ``mpc_observation``: ``deep_db_mpc_tracklet_id``/``trkid``/``trksub``,
      plus the designation (``deep_db_mpc_object_id``/``provid``/...) when
      deep-db attributes the tracklet to one object.
    * ``mpchecker``: ``deep_db_mpc_object_id`` and its designation.
    * ``mpc_linked``: derived from ``mpc_observation`` rows (``deep-asteroids
      link``): every group of MPC tracklets that are one object's night split
      across several ``trkid``\ s, refit over the union of their observations
      (a tracklet that merged with nothing is a group of one, with its own fit).
      It has no single ``trkid``: ``known_tracklet_member`` lists the
      ``mpc_observation`` rows each one was built from, which carry the
      ``trkid``/``trksub``/designation. ``n_members`` counts them,
      ``designations`` lists their distinct designations (comma-joined) and
      ``n_designations`` counts them; when exactly one, ``provid``/``permid``/
      ``name`` are set, and more than one flags two different known objects
      whose tracks agree (reported, not split).
    """

    __tablename__ = "known_tracklet"
    __table_args__ = (
        CheckConstraint(f"source IN ({_sql_list(SOURCES)})", name="ck_known_tracklet_source"),
        CheckConstraint(
            f"component IS NULL OR component IN ({_sql_list(SYNTHETIC_COMPONENTS)})",
            name="ck_known_tracklet_component",
        ),
        CheckConstraint(f"flag IS NULL OR flag IN ({_sql_list(FLAGS)})", name="ck_known_tracklet_flag"),
        # a fit is all-or-nothing, and missing exactly when a no-fit flag is set
        CheckConstraint(
            f"((flag IS NULL OR flag IN ({_sql_list(SUSPECT_FIT_FLAGS)})) "
            "AND epoch IS NOT NULL AND ra_epoch IS NOT NULL AND dec_epoch IS NOT NULL "
            "AND vra IS NOT NULL AND vdec IS NOT NULL) OR "
            f"(flag IN ({_sql_list(NO_FIT_FLAGS)}) AND epoch IS NULL AND ra_epoch IS NULL "
            "AND dec_epoch IS NULL AND vra IS NULL AND vdec IS NULL)",
            name="ck_known_tracklet_fit_xor_flag",
        ),
        # natural keys, one per source
        Index(
            "uq_known_tracklet_synthetic", "night", "synthetic_source", "orbit_id", "component",
            unique=True, **_where("source = 'synthetic'"),
        ),
        Index(
            "uq_known_tracklet_mpc_observation", "night", "deep_db_mpc_tracklet_id",
            unique=True, **_where("source = 'mpc_observation'"),
        ),
        Index(
            "uq_known_tracklet_mpchecker", "night", "deep_db_mpc_object_id",
            unique=True, **_where("source = 'mpchecker'"),
        ),
        Index("ix_known_tracklet_source_night", "source", "night"),
    )

    id = Column(Integer, primary_key=True)
    source = Column(String, nullable=False)
    night = Column(Integer, nullable=False, index=True)

    # the fit (NULL when flagged)
    epoch = Column(Float)  # MJD TAI
    ra_epoch = Column(Float)  # deg, [0, 360)
    dec_epoch = Column(Float)  # deg
    vra = Column(Float)  # deg/day, dRA/dt
    vdec = Column(Float)  # deg/day
    t_min = Column(Float, nullable=False)  # MJD TAI
    t_max = Column(Float, nullable=False)  # MJD TAI
    n_points = Column(Integer, nullable=False)
    max_residual_arcsec = Column(Float)
    flag = Column(String)

    # synthetic
    deep_db_synthetic_object_id = Column(Integer)
    synthetic_source = Column(String)  # deep-db synthetic_object.source: 'tno' | 'asteroid'
    orbit_id = Column(Integer, index=True)
    component = Column(String)

    # mpc_observation
    deep_db_mpc_tracklet_id = Column(Integer)
    trkid = Column(String, index=True)
    trksub = Column(String)
    statuses = Column(String)  # sorted distinct mpc_observation.status values, comma-joined

    # mpc_observation (when attributed) and mpchecker
    deep_db_mpc_object_id = Column(Integer)
    provid = Column(String, index=True)
    permid = Column(String)
    name = Column(String)

    # mpchecker: how many fitted points landed on a detector
    n_on_chip = Column(Integer)

    # synthetic (primary/companion) and mpchecker: the median magnitude of the night's points (deep-db's
    # ephemeris ``mag``/``v_mag``), the truth a recovery is compared with. NULL for the others.
    mag = Column(Float)

    # mpc_linked: the group this row was built from
    n_members = Column(Integer)
    n_designations = Column(Integer)
    designations = Column(String)

    deep_db_sha = Column(String)
    created_at = Column(DateTime, nullable=False)

    @property
    def ra_0(self):
        """RA extrapolated to MJD 0 (TAI) -- find-asteroids' ``Tracklet.ra_0`` convention."""
        return None if self.epoch is None else self.ra_epoch - self.vra * self.epoch

    @property
    def dec_0(self):
        """Dec extrapolated to MJD 0 (TAI) -- find-asteroids' ``Tracklet.dec_0`` convention."""
        return None if self.epoch is None else self.dec_epoch - self.vdec * self.epoch


class KnownTrackletMember(Base):
    """One row per ``mpc_observation`` tracklet that went into an
    ``mpc_linked`` one: ``linked_id`` is the merged row, ``member_id`` the
    per-``trkid`` row (which carries ``trkid``/``trksub``/``provid`` and the
    deep-db ids). A member is in at most one group."""

    __tablename__ = "known_tracklet_member"
    __table_args__ = (UniqueConstraint("member_id", name="uq_known_tracklet_member_member"),)

    id = Column(Integer, primary_key=True)
    linked_id = Column(Integer, ForeignKey("known_tracklet.id"), nullable=False, index=True)
    member_id = Column(Integer, ForeignKey("known_tracklet.id"), nullable=False)
