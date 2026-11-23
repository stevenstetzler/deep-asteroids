"""
Migrate deep-asteroids' own tables from a SQLite file to Postgres (#47).

SQLite's WAL mode (``database.get_db()``) is a stopgap for concurrent writers: it needs every
writer on one host, so a Slurm array spread across nodes can't use it reliably (see that function's
own docstring). Postgres removes that limit. The codebase is already dialect-generic --
``get_db()``/``create_db()`` just call ``create_engine(url)``, with SQLite-only behavior (the busy
timeout, the WAL pragma) gated behind an ``is_sqlite`` check, and ``database/tracklets.py``'s partial
indexes already define both a ``sqlite_where`` and a ``postgresql_where`` -- so this is a data copy,
not a schema change.

For each deep-asteroids/find-asteroids table (``Base.metadata.sorted_tables``, parent-before-child so
foreign keys are always satisfied), streams rows from the SQLite file in chunks and bulk-loads them
into Postgres with a ``COPY ... FROM STDIN WITH (FORMAT csv)`` (far faster than row-by-row inserts
for the ~73M rows of the full-survey database). Each table's identity/serial sequence is reset to
``max(id)`` afterwards, since the explicit ids copied over bypass it. Row counts are verified source
vs. destination per table as it goes.

The destination database must already exist (``createdb``); its schema is (re-)created here via the
existing ``deep_asteroids.database.create_db(force=True)``, pointed at ``--postgres-url``.

Usage (compute node; reads the SQLite file read-only, writes to Postgres):

    export LD_LIBRARY_PATH="$(pg_config --libdir):$LD_LIBRARY_PATH"  # psycopg2 needs libpq.so.5
    python results/migrate_to_postgres.py \
        --sqlite-path /path/to/deep_asteroids_full_survey.sqlite \
        --postgres-url postgresql://user@localhost:55432/deep_asteroids
"""
import argparse
import io
import sqlite3
import time


def _csv_field(value):
    """One CSV field: unquoted empty for NULL (``None``), else always double-quoted (even an empty
    string) so an empty string is never mistaken for NULL on the Postgres side -- the default "quote
    only if it contains a special character" behavior can't tell the two apart for an empty value."""
    if value is None:
        return ""
    return '"' + str(value).replace('"', '""') + '"'


def _rows_to_csv(rows):
    out = io.StringIO()
    for row in rows:
        out.write(",".join(_csv_field(v) for v in row))
        out.write("\n")
    out.seek(0)
    return out


def migrate_table(sqlite_conn, pg_conn, table, chunk_size):
    columns = [c.name for c in table.columns]
    column_list = ", ".join(f'"{c}"' for c in columns)
    select_sql = f"SELECT {column_list} FROM \"{table.name}\""

    (source_n,) = sqlite_conn.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()
    if source_n == 0:
        print(f"  {table.name}: 0 rows, skipped")
        return 0

    cur = sqlite_conn.execute(select_sql)
    copied = 0
    with pg_conn.cursor() as pg_cur:
        while True:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            pg_cur.copy_expert(f'COPY "{table.name}" ({column_list}) FROM STDIN WITH (FORMAT csv)',
                               _rows_to_csv(rows))
            copied += len(rows)
            print(f"  {table.name}: {copied:,}/{source_n:,}", end="\r")
        # reset the identity/serial sequence past the explicit ids just copied, if the table has one
        pg_cur.execute(
            "SELECT pg_get_serial_sequence(%s, 'id')", (table.name,))
        (sequence,) = pg_cur.fetchone()
        if sequence:
            pg_cur.execute(f'SELECT setval(%s, (SELECT COALESCE(MAX(id), 1) FROM "{table.name}"))', (sequence,))
    pg_conn.commit()

    with pg_conn.cursor() as pg_cur:
        pg_cur.execute(f'SELECT COUNT(*) FROM "{table.name}"')
        (dest_n,) = pg_cur.fetchone()
    status = "OK" if dest_n == source_n else "MISMATCH"
    print(f"  {table.name}: {copied:,} copied, source {source_n:,} / destination {dest_n:,} ({status})")
    if dest_n != source_n:
        raise RuntimeError(f"{table.name}: row count mismatch after copy (source {source_n:,}, destination {dest_n:,})")
    return copied


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sqlite-path", required=True, help="The deep-asteroids SQLite file to migrate")
    p.add_argument("--postgres-url", required=True,
                   help="Destination Postgres URL (e.g. postgresql://user@localhost:55432/deep_asteroids); "
                        "the database must already exist (createdb)")
    p.add_argument("--chunk-size", type=int, default=50_000, help="Rows read from SQLite and COPYed at a time")
    p.add_argument("--tables", nargs="*", default=None,
                   help="Only migrate these tables (by name), in the order given; default: every table")
    return p.parse_args(argv)


def main(argv=None):
    import os

    import psycopg2

    args = parse_args(argv)
    os.environ["DEEP_ASTEROIDS_DB"] = args.postgres_url
    from deep_asteroids.database import create_db
    from deep_asteroids.database.models import Base

    print(f"creating schema on {args.postgres_url}")
    create_db(force=True)

    sqlite_conn = sqlite3.connect(f"file:{args.sqlite_path}?mode=ro&immutable=1", uri=True)
    pg_conn = psycopg2.connect(args.postgres_url)
    try:
        tables = Base.metadata.sorted_tables
        if args.tables:
            by_name = {t.name: t for t in tables}
            tables = [by_name[name] for name in args.tables]
        total = 0
        started = time.monotonic()
        for table in tables:
            total += migrate_table(sqlite_conn, pg_conn, table, args.chunk_size)
        print(f"migrated {total:,} rows across {len(tables)} table(s) in {time.monotonic() - started:.0f}s")
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
