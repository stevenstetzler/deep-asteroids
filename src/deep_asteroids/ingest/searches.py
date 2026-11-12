"""
Ingest search definitions from deep-db into ``search_definition`` /
``search_definition_exposure``.

One row per `(night, field, detector)` triple deep-db knows about -- one
CCD of one pointing group over one night, the unit find-asteroids searches.
Filtering by band, science field or minimum exposure count is deferred to
the search stage (migration decision 3): every triple gets a row here,
recording `band`/`n_exposures` so that filtering is a later query, not an
ingest-time decision.

Exposures with no field (`exposure.field_id IS NULL`, a handful survey-
wide) form no pointing group and are skipped -- there's no (night, field,
detector) triple to group them into, not a science-field filtering choice.

Ingest is idempotent per night: a night that already has rows is skipped,
unless ``force``, which deletes and re-inserts it.
"""
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import groupby

from sqlalchemy import delete, insert, select, text

from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure

log = logging.getLogger(__name__)

SEARCH_POINTS = """
SELECT n.night, f.name, d.number, d.name,
       e.id, de.id, e.expnum, e.band, e.midpoint_mjd,
       de.ra_00, de.dec_00, de.ra_01, de.dec_01, de.ra_10, de.dec_10, de.ra_11, de.dec_11
FROM detector_exposure de
JOIN detector d ON d.id = de.detector_id
JOIN exposure e ON e.id = de.exposure_id
JOIN night n ON n.id = e.night_id
JOIN field f ON f.id = e.field_id
WHERE 1=1 {nights}
ORDER BY n.night, f.name, d.number, e.midpoint_mjd
"""


@dataclass
class SearchSummary:
    nights: list = field(default_factory=list)
    skipped_nights: list = field(default_factory=list)
    n_searches: int = 0
    n_exposures: int = 0
    n_mixed_band: int = 0

    def format(self):
        header = (
            f"searches: {len(self.nights)} night(s) ingested, {len(self.skipped_nights)} skipped "
            "(already present; --force to replace)"
        )
        lines = [header]
        if self.nights:
            lines.append(f"  {self.n_searches:,} search(es), {self.n_exposures:,} exposure-detector row(s)")
            if self.n_mixed_band:
                lines.append(f"  {self.n_mixed_band:,} search(es) mix more than one band (band = the most common)")
        return "\n".join(lines)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _run_id(field_name, detector_number, night):
    return f"{field_name}-ccd{detector_number:02d}-{night}"


def deep_db_nights(deep_engine):
    with deep_engine.connect() as conn:
        return sorted(r[0] for r in conn.execute(text("SELECT night FROM night WHERE night IS NOT NULL")))


def search_rows(deep_engine, nights=None):
    """``(search_rows, exposure_rows, n_mixed_band)`` for ``nights`` (every
    night with pointing geometry, if ``None``): one `search_definition` dict
    per `(night, field, detector)` and the `search_definition_exposure`
    dicts for it (each missing ``search_definition_id``, filled in once the
    parent row's id is known)."""
    clause = "" if nights is None else "AND n.night IN :nights"
    query = text(SEARCH_POINTS.format(nights=clause))
    params = {}
    if nights is not None:
        from sqlalchemy import bindparam

        query = query.bindparams(bindparam("nights", expanding=True))
        params = {"nights": list(nights)}

    with deep_engine.connect() as conn:
        points = conn.execute(query, params).fetchall()

    search_rows_, n_mixed_band = [], 0
    for (night, field_name, detector_number, detector_name), group in groupby(
        points, key=lambda r: (r[0], r[1], r[2], r[3])
    ):
        group = list(group)
        bands = Counter(r[7] for r in group if r[7] is not None)
        band = max(sorted(bands), key=lambda b: bands[b]) if bands else None
        if len(bands) > 1:
            n_mixed_band += 1
        mjds = [r[8] for r in group]
        search_rows_.append({
            "night": night, "field": field_name, "detector_number": detector_number,
            "detector_name": detector_name, "run_id": _run_id(field_name, detector_number, night),
            "band": band, "n_exposures": len(group), "t_start_mjd": min(mjds), "t_end_mjd": max(mjds),
            "_exposures": [
                {
                    "deep_db_exposure_id": int(r[4]), "deep_db_detector_exposure_id": int(r[5]),
                    "expnum": int(r[6]), "midpoint_mjd": r[8],
                    "ra_00": r[9], "dec_00": r[10], "ra_01": r[11], "dec_01": r[12],
                    "ra_10": r[13], "dec_10": r[14], "ra_11": r[15], "dec_11": r[16],
                }
                for r in group
            ],
        })
    return search_rows_, n_mixed_band


def ingest_searches(session, deep_engine, nights=None, force=False, deep_db_sha=None):
    """Ingest search definitions for ``nights`` (default: every deep-db
    night). Returns a ``SearchSummary``. Commits."""
    summary = SearchSummary()
    requested = sorted(set(nights)) if nights else deep_db_nights(deep_engine)
    existing = set(session.scalars(select(SearchDefinition.night).distinct()))
    if force:
        todo = requested
        if todo:
            # a bulk Core delete() doesn't trigger the ORM cascade, so the
            # child rows need their own explicit delete
            session.execute(delete(SearchDefinitionExposure).where(SearchDefinitionExposure.search_definition_id.in_(
                select(SearchDefinition.id).where(SearchDefinition.night.in_(todo))
            )))
            session.execute(delete(SearchDefinition).where(SearchDefinition.night.in_(todo)))
    else:
        todo = [n for n in requested if n not in existing]
        summary.skipped_nights = [n for n in requested if n in existing]
    summary.nights = todo
    if not todo:
        session.commit()
        return summary

    rows, summary.n_mixed_band = search_rows(deep_engine, nights=todo)
    created_at = _now()
    for row in rows:
        exposures = row.pop("_exposures")
        row["deep_db_sha"] = deep_db_sha
        row["created_at"] = created_at
        search_id = session.execute(insert(SearchDefinition).returning(SearchDefinition.id), row).scalar_one()
        for exposure in exposures:
            exposure["search_definition_id"] = search_id
        if exposures:
            session.execute(insert(SearchDefinitionExposure), exposures)
        summary.n_exposures += len(exposures)
    session.commit()

    summary.n_searches = len(rows)
    log.info("%s", summary.format())
    return summary
