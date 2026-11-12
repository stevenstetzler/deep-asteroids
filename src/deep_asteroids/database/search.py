"""
Search definitions: one `(night, field, detector)` triple -- one CCD of
one pointing group over one night -- is "one search" in find-asteroids
terms, materialized once per band present plus one spanning every band.
See ``ingest/searches.py``.
"""
from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from deep_asteroids.database.models import Base


class SearchDefinition(Base):
    """One `(night, field, detector)` triple deep-db knows about, independent
    of whether it's actually worth searching -- science-field/minimum-
    exposure filtering is deferred to the search stage.

    **Band is part of the grouping, not just a descriptive column.** Each
    triple gets one row per band actually present among its exposures
    (`band` set, scoped to that band's own exposures), plus always one
    more row with `band = NULL` spanning *every* exposure of the triple
    regardless of band -- the natural key is `(night, field,
    detector_number, band)`. A triple with a single band therefore gets
    exactly two rows (the band and the duplicate `NULL` one); a triple
    that mixes bands (mostly junk/placeholder fields, e.g. ``--``) gets
    one row per band plus the `NULL` union -- see ``SearchSummary``'s
    `n_multi_band_triples` for how many an ingest run saw.

    `run_id` is find-asteroids' own `Search.run_id` convention for the
    `band = NULL` row, matching salad's
    `DeepDecamPointingGroupStrategy.run_id_for()` exactly:
    `deep-decam-<field>-ccd<detector_number>-<night>` (e.g.
    ``deep-decam-A0b-ccd4-20190402`` -- detector_number is *not*
    zero-padded). A per-band row (salad has no equivalent) appends
    `-<band>`.
    """

    __tablename__ = "search_definition"
    __table_args__ = (
        UniqueConstraint(
            "night", "field", "detector_number", "band", name="uq_search_definition_night_field_detector_band",
        ),
        Index("ix_search_definition_night_field", "night", "field"),
    )

    id = Column(Integer, primary_key=True)
    night = Column(Integer, nullable=False, index=True)
    field = Column(String, nullable=False)
    detector_number = Column(Integer, nullable=False)
    detector_name = Column(String, nullable=False)
    run_id = Column(String, nullable=False, unique=True)
    band = Column(String)
    n_exposures = Column(Integer, nullable=False)
    t_start_mjd = Column(Float, nullable=False)  # UTC, exposure.midpoint_mjd
    t_end_mjd = Column(Float, nullable=False)
    deep_db_sha = Column(String)
    created_at = Column(DateTime, nullable=False)

    exposures = relationship(
        "SearchDefinitionExposure", back_populates="search_definition",
        cascade="all, delete-orphan", order_by="SearchDefinitionExposure.midpoint_mjd",
    )


class SearchDefinitionExposure(Base):
    """One of a `SearchDefinition`'s exposures: the time baseline and chip
    footprint (corners) a later image-selection/photometry box test reads,
    instead of salad's separate `DetectorExposureFootprint` cache. Refers to
    deep-db by id (no foreign key: a separate database) -- points/pixels are
    re-read from deep-db/Butler by these ids, never stored here."""

    __tablename__ = "search_definition_exposure"
    __table_args__ = (
        UniqueConstraint(
            "search_definition_id", "deep_db_detector_exposure_id",
            name="uq_search_definition_exposure_detector_exposure",
        ),
    )

    id = Column(Integer, primary_key=True)
    search_definition_id = Column(Integer, ForeignKey("search_definition.id"), nullable=False, index=True)
    deep_db_exposure_id = Column(Integer, nullable=False)
    deep_db_detector_exposure_id = Column(Integer, nullable=False)
    expnum = Column(Integer, nullable=False)
    midpoint_mjd = Column(Float, nullable=False)  # UTC
    # chip footprint corners (deg), copied from deep-db's detector_exposure
    ra_00 = Column(Float)
    dec_00 = Column(Float)
    ra_01 = Column(Float)
    dec_01 = Column(Float)
    ra_10 = Column(Float)
    dec_10 = Column(Float)
    ra_11 = Column(Float)
    dec_11 = Column(Float)

    search_definition = relationship("SearchDefinition", back_populates="exposures")
