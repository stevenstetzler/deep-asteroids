"""
One row per find-asteroids search run by ``search/run.py``: what was run
(parameters, seed, the time offset applied) and with what (library versions),
and how long each stage took. Joins to find-asteroids' ``search`` row and our
``search_definition`` by ``run_id``.
"""
from sqlalchemy import Column, DateTime, Float, Integer, String

from deep_asteroids.database.models import Base


class SearchRun(Base):
    """``run_id`` is both ``search.run_id`` and ``search_definition.run_id``
    (a search run is always one search definition). Times are wall seconds."""

    __tablename__ = "search_run"

    id = Column(Integer, primary_key=True)
    run_id = Column(String, nullable=False, unique=True)
    # search parameters (find-asteroids' own copy is in its `search` row)
    seed = Column(Integer, nullable=False)
    shutter_offset_seconds = Column(Float, nullable=False)  # added to deep-db's midpoint to label each exposure
    threshold = Column(Float, nullable=False)  # detection threshold (sigma)
    polarity = Column(String, nullable=False)
    n_exposures = Column(Integer, nullable=False)
    n_detections = Column(Integer, nullable=False)  # rows in the stacked catalog
    n_results = Column(Integer)
    # what ran
    find_asteroids_version = Column(String)
    numba_version = Column(String)
    numpy_version = Column(String)
    n_cpus = Column(Integer)
    # cost
    detect_seconds = Column(Float)  # 0 if every catalog already existed
    search_seconds = Column(Float)
    ingest_seconds = Column(Float)
    created_at = Column(DateTime, nullable=False)
