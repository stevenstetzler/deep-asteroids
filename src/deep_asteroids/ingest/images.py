"""
Ingest image URIs from the Butler registry into ``image``.

Ported from salad's ``DeepDecamImageIngestor._scan_registry()``
(``salad/src/salad/ingest.py``): same registry SQL, same duplicate-
``(visit, detector)`` resolution by the owning RUN's rank in
``DEEP/<night>/<field>``'s own collection chain, same deterministic
(lowest ``dataset.id``) fallback when that collection isn't found.
Doesn't port ``_scan_filesystem()`` (the ``os.walk`` fallback) -- the
registry path is the only one exercised on klone.

**Always scoped per (night, field) pointing group**, not run as one
global query: the real registry has thousands of RUN collections from
repeated reprocessing (526,900 distinct (visit, detector) pairs for
``differenceExp`` alone, across 16M+ tagged rows), so resolving
duplicates correctly needs each pointing group's own collection-chain
precedence -- the "lowest dataset.id" fallback is only reliable within
one pointing group's chain, not across the whole survey. Pointing
groups to process come from deep-db directly (every ``(night, field)``
it has), optionally narrowed by ``--night``/``--field``.

**Fixed a Postgres/SQLite portability bug carried over from salad**: the
recursive CTE's base case selected ``collection_chain.position`` (a
``smallint`` in the real registry) directly, while the recursive case
computes an ``integer`` expression (``rank * 100000 + position``) --
Postgres rejects a ``UNION ALL`` whose branches don't agree on type
(``column 3 has type smallint in non-recursive term but type integer
overall``), where SQLite silently allows it. Salad's own test suite only
ever exercised this against an SQLite fake registry, so it never caught
it. Fixed here by casting the base case's `position` to `integer`
explicitly -- verified against the real registry (``psql -h
epyc.astro.washington.edu deep_klone_2``).

Unlike salad's generic, survey-wide ``image`` table, each row here is
keyed by deep-db's own ``detector_exposure.id`` (see ``database/images.py``
for why) rather than a ``search_definition_exposure`` row.

No pixel reads here -- this only records URIs/paths (#8/#5 read pixels).
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import bindparam, create_engine, delete, insert, select, text

from deep_asteroids.database.images import Image

log = logging.getLogger(__name__)

DEEP_DB_EXPOSURES = """
SELECT e.expnum, d.number, e.id, de.id
FROM detector_exposure de
JOIN detector d ON d.id = de.detector_id
JOIN exposure e ON e.id = de.exposure_id
JOIN night n ON n.id = e.night_id
JOIN field f ON f.id = e.field_id
WHERE n.night = :night AND f.name = :field
"""

# the base case selects collection_chain.position (smallint in the real
# registry) straight into a column the recursive case computes as an
# integer expression -- Postgres requires both UNION ALL branches to
# agree on type, so the base case casts explicitly (harmless on SQLite)
CHAIN_RANK_CTE = """
WITH RECURSIVE chain_rank(collection_id, type, rank) AS (
    SELECT c.collection_id, c.type, CAST(cc.position AS INTEGER)
    FROM collection_chain cc
    JOIN collection c ON c.collection_id = cc.child
    WHERE cc.parent = :root_collection_id
  UNION ALL
    SELECT c2.collection_id, c2.type, cr.rank * 100000 + cc2.position
    FROM chain_rank cr
    JOIN collection_chain cc2 ON cc2.parent = cr.collection_id
    JOIN collection c2 ON c2.collection_id = cc2.child
    WHERE cr.type = 3
)
"""


@dataclass
class ImageSummary:
    groups: list = field(default_factory=list)
    skipped_groups: list = field(default_factory=list)
    n_written: int = 0
    n_duplicates_resolved: int = 0
    n_unmatched: int = 0

    def format(self):
        header = (
            f"images: {len(self.groups)} (night, field) group(s) ingested, {len(self.skipped_groups)} skipped "
            "(already present; --force to replace)"
        )
        lines = [header]
        if self.groups:
            lines.append(f"  {self.n_written:,} image(s) written")
            if self.n_duplicates_resolved:
                lines.append(f"  {self.n_duplicates_resolved:,} duplicate (visit, detector) pair(s) resolved")
            if self.n_unmatched:
                lines.append(
                    f"  {self.n_unmatched:,} registry row(s) had no matching deep-db detector-exposure -- skipped"
                )
        return "\n".join(lines)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def deep_db_pointing_groups(deep_engine, nights=None, fields=None):
    """Every ``(night, field)`` pair deep-db has, optionally filtered."""
    clauses, params = [], {}
    if nights:
        clauses.append("n.night IN :nights")
        params["nights"] = list(nights)
    if fields:
        clauses.append("f.name IN :fields")
        params["fields"] = list(fields)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = text(f"""
        SELECT DISTINCT n.night, f.name
        FROM exposure e JOIN night n ON n.id = e.night_id JOIN field f ON f.id = e.field_id
        {where}
        ORDER BY n.night, f.name
    """)
    for name, values in (("nights", nights), ("fields", fields)):
        if values:
            query = query.bindparams(bindparam(name, expanding=True))
    with deep_engine.connect() as conn:
        return conn.execute(query, params).all()


def _deep_db_detector_exposures(deep_engine, night, field):
    """``{(expnum, detector_number): (deep_db_exposure_id, deep_db_detector_exposure_id)}``
    for one pointing group."""
    with deep_engine.connect() as conn:
        rows = conn.execute(text(DEEP_DB_EXPOSURES), {"night": night, "field": field}).all()
    return {(expnum, detector_number): (exposure_id, detector_exposure_id)
            for expnum, detector_number, exposure_id, detector_exposure_id in rows}


def _root_collection_id(conn, night, field):
    return conn.execute(
        text("SELECT collection_id FROM collection WHERE name = :name AND type = 3"),
        {"name": f"DEEP/{night}/{field}"},
    ).scalar_one_or_none()


def _tag_association_table(conn, product_type):
    table = conn.execute(
        text("SELECT tag_association_table FROM dataset_type WHERE name = :name"),
        {"name": product_type},
    ).scalar_one_or_none()
    if table is not None and not re.fullmatch(r"[A-Za-z0-9_]+", table):
        # table is registry-controlled, not user input, but it's about to be
        # interpolated into a SQL string (identifiers can't be bound as
        # params), so validate its shape regardless.
        raise ValueError(f"unexpected tag_association_table name {table!r}")
    return table


def _scan_one_group(conn, product_type, table, root_collection_id, visits):
    """Yield ``(visit, detector, path)`` for one product type within one
    pointing group's collection (or unscoped, if ``root_collection_id`` is
    ``None``), resolving a duplicate ``(visit, detector)`` by the owning
    RUN's rank in the chain when scoped, else deterministically (lowest
    ``dataset.id``)."""
    cte, rank_join, rank_select = "", "", "NULL"
    if root_collection_id is not None:
        cte = CHAIN_RANK_CTE
        rank_join = "JOIN dataset d ON d.id = t.dataset_id JOIN chain_rank cr ON cr.collection_id = d.run_id"
        rank_select = "cr.rank"

    where_extra = " AND t.visit IN :visits" if visits is not None else ""
    query = text(f"""
        {cte}
        SELECT t.visit, t.detector, f.path, t.dataset_id, {rank_select} AS rank
        FROM dataset_type dt
        JOIN {table} t ON t.dataset_type_id = dt.id
        JOIN file_datastore_records f ON f.dataset_id = t.dataset_id
        {rank_join}
        WHERE dt.name = :name{where_extra}
        ORDER BY t.visit, t.detector
    """)
    params = {"name": product_type}
    if root_collection_id is not None:
        params["root_collection_id"] = root_collection_id
    if visits is not None:
        query = query.bindparams(bindparam("visits", expanding=True))
        params["visits"] = list(visits)

    groups = {}
    for visit, detector, path, dataset_id, rank in conn.execute(query, params):
        groups.setdefault((visit, detector), {})[dataset_id] = (rank, path)

    n_duplicates = 0
    rows = []
    for (visit, detector), per_dataset in groups.items():
        if len(per_dataset) > 1:
            n_duplicates += 1
        if root_collection_id is not None:
            _dsid, (_rank, path) = min(per_dataset.items(), key=lambda kv: (kv[1][0], str(kv[0])))
        else:
            _dsid, (_rank, path) = min(per_dataset.items(), key=lambda kv: str(kv[0]))
        rows.append((visit, detector, path))
    return rows, n_duplicates


def ingest_images(session, deep_engine, registry_uri, repo_dir, product_types, nights=None, fields=None,
                  force=False, deep_db_sha=None):
    """Ingest image URIs for every ``(night, field)`` pointing group
    deep-db has (optionally filtered by ``nights``/``fields``) and each of
    ``product_types``. ``repo_dir`` is the Butler repo root the registry's
    ``path`` column is relative to (e.g. ``/gscratch/dirac/DEEP/repo``) --
    joined with each row's ``path`` to make a directly-openable URI, the
    same convention salad's ``DeepDecamImageIngestor`` uses. Returns an
    ``ImageSummary``. Commits."""
    from pathlib import Path

    repo_dir = Path(repo_dir)
    requested = deep_db_pointing_groups(deep_engine, nights=nights, fields=fields)
    existing = set(session.execute(select(Image.night, Image.field).distinct()).all())
    if force:
        todo = requested
        if todo:
            for night, field_name in todo:
                session.execute(delete(Image).where(Image.night == night, Image.field == field_name))
    else:
        todo = [g for g in requested if g not in existing]
    summary = ImageSummary(groups=todo, skipped_groups=[g for g in requested if g not in todo])
    if not todo:
        session.commit()
        return summary

    registry_engine = create_engine(registry_uri)
    created_at = _now()
    try:
        with registry_engine.connect() as conn:
            for night, field_name in todo:
                lookup = _deep_db_detector_exposures(deep_engine, night, field_name)
                root_collection_id = _root_collection_id(conn, night, field_name)
                if root_collection_id is None:
                    log.warning(
                        "no CHAINED collection named 'DEEP/%s/%s' in the registry -- falling back to "
                        "deterministic (lowest dataset.id) duplicate resolution", night, field_name,
                    )
                visits = sorted({expnum for expnum, _ in lookup}) or None

                rows_to_write = []
                for product_type in product_types:
                    table = _tag_association_table(conn, product_type)
                    if table is None:
                        log.warning("no dataset_type named %r in the registry -- skipping", product_type)
                        continue
                    rows, n_dup = _scan_one_group(conn, product_type, table, root_collection_id, visits)
                    summary.n_duplicates_resolved += n_dup
                    for visit, detector, path in rows:
                        identity = lookup.get((visit, detector))
                        if identity is None:
                            summary.n_unmatched += 1
                            continue
                        exposure_id, detector_exposure_id = identity
                        rows_to_write.append({
                            "night": night, "field": field_name, "detector_number": detector,
                            "expnum": visit, "deep_db_exposure_id": exposure_id,
                            "deep_db_detector_exposure_id": detector_exposure_id,
                            "product_type": product_type, "uri": str(repo_dir / path),
                            "deep_db_sha": deep_db_sha, "created_at": created_at,
                        })
                if rows_to_write:
                    session.execute(insert(Image), rows_to_write)
                    summary.n_written += len(rows_to_write)
        session.commit()
    finally:
        registry_engine.dispose()
    return summary
