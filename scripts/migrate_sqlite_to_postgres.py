"""One-time, non-destructive import into an EMPTY PostgreSQL application schema.

Stop SQLite writers first. Media stays in the same protected storage directory.
No tables are truncated and the original SQLite file is opened read-only.
"""
import argparse
from pathlib import Path
import sqlite3
from peewee import PostgresqlDatabase
from app.db.database import database_proxy, init_database, create_tables, close_database

TABLES = (
    "devices", "role_events", "messages", "static_locations", "media",
    "live_location_sessions", "location_points", "bans", "consent_events", "message_receipts",
)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def migrate(source: Path) -> dict[str, int]:
    if not isinstance(database_proxy.obj, PostgresqlDatabase):
        raise RuntimeError("Migration target must be PostgreSQL")
    uri = source.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as old, database_proxy.atomic():
        old.execute("BEGIN")
        if old.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")
        available = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if set(TABLES) - available:
            raise RuntimeError("Source schema is incomplete; migration requires the current SQLite schema")
        if any(database_proxy.execute_sql(f"SELECT count(*) FROM {quote(t)}").fetchone()[0] for t in TABLES):
            raise RuntimeError("Target is not empty; refusing to overwrite data")
        counts = {}
        for table in TABLES:
            cursor = old.execute(f"SELECT * FROM {quote(table)}")
            columns = [item[0] for item in cursor.description]
            target = {c.name: c.data_type.lower() for c in database_proxy.get_columns(table)}
            if set(columns) - target.keys():
                raise RuntimeError(f"Unknown source columns in {table}")
            sql = f"INSERT INTO {quote(table)} ({','.join(map(quote, columns))}) VALUES ({','.join(['%s'] * len(columns))})"
            count = 0
            for values in cursor:
                values = [bool(value) if target[name] == 'boolean' and value is not None else value
                          for name, value in zip(columns, values)]
                database_proxy.execute_sql(sql, values)
                count += 1
            actual = database_proxy.execute_sql(f"SELECT count(*) FROM {quote(table)}").fetchone()[0]
            if actual != count:
                raise RuntimeError(f"Row count mismatch for {table}")
            counts[table] = count
        return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    init_database()
    try:
        create_tables()
        print(migrate(args.source))
    finally:
        close_database()


if __name__ == "__main__":
    main()
