"""
Image URIs: one Butler dataset path per deep-db detector-exposure and
product type (e.g. ``differenceExp``). See ``ingest/images.py``.
"""
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from deep_asteroids.database.models import Base


class Image(Base):
    """One Butler dataset's path for one deep-db detector-exposure and
    product type.

    Keyed by deep-db's own ``detector_exposure.id`` (not a
    ``search_definition_exposure`` row): a ``(night, field, detector)``
    triple can have several ``search_definition_exposure`` rows for the
    same real exposure -- one per band present, plus the ``band = NULL``
    union (see ``database/search.py``) -- and they'd all want the exact
    same image URI, so keying on the real deep-db id avoids writing
    duplicate rows for them. Join on
    ``search_definition_exposure.deep_db_detector_exposure_id`` to
    recover "this search's images".

    No pixel reads here -- this only records the URI (#8/#5 read pixels).
    """

    __tablename__ = "image"
    __table_args__ = (
        UniqueConstraint(
            "deep_db_detector_exposure_id", "product_type", name="uq_image_detector_exposure_product",
        ),
    )

    id = Column(Integer, primary_key=True)
    night = Column(Integer, nullable=False, index=True)
    field = Column(String, nullable=False)
    detector_number = Column(Integer, nullable=False)
    expnum = Column(Integer, nullable=False)
    deep_db_exposure_id = Column(Integer, nullable=False)
    deep_db_detector_exposure_id = Column(Integer, nullable=False, index=True)
    product_type = Column(String, nullable=False)
    uri = Column(String, nullable=False)
    deep_db_sha = Column(String)
    created_at = Column(DateTime, nullable=False)
