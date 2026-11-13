"""
Per-result summary statistics. See ``crossmatch/statistics.py``.
"""
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer

from deep_asteroids.database.models import Base


class ResultStatistics(Base):
    """One row per find-asteroids ``result``: numbers computed from its own
    ``gathered`` points and ``tracklet``, independent of any cross-match.

    ``median_residual_arcsec`` is the median great-circle separation between
    each gathered point and the result's own tracklet evaluated at that
    point's time (the quantity that separates real moving objects, with
    residuals of a few tenths of an arcsec, from noise that happens to line up,
    with arcsec-scale ones); ``n_residual_outliers`` counts the points beyond
    ``statistics.MAX_RESIDUAL_ARCSEC``. ``speed`` is ``hypot(vra, vdec)`` in
    deg/day (find-asteroids' convention, with no cos(dec)), directly comparable
    to a search's ``velocity_0``/``velocity_1``; ``sigma_v`` is
    ``hypot(sigma_vra, sigma_vdec)``. ``median_mag`` is the median of the
    gathered points' magnitudes (NULL if none has one). A result with no
    gathered points has ``n_gathered`` 0 and NULL residual/magnitude.
    """

    __tablename__ = "result_statistics"

    id = Column(Integer, primary_key=True)
    result_id = Column(Integer, ForeignKey("result.id"), nullable=False, unique=True)
    n_gathered = Column(Integer, nullable=False)
    n_times = Column(Integer, nullable=False)  # distinct times among the gathered points
    median_residual_arcsec = Column(Float)
    n_residual_outliers = Column(Integer, nullable=False)
    speed = Column(Float, nullable=False)
    sigma_v = Column(Float)
    median_mag = Column(Float)
    computed_at = Column(DateTime, nullable=False)
