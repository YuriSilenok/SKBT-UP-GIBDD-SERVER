"""Synthetic-data-only demonstration on a small VDS; production uses PostgreSQL.

Start only with GIBDD_STAGING=1 and a dedicated GIBDD_STAGING_DIR.
SQLite here is for functional HTTP/UI testing; PostgreSQL concurrency is tested separately.
"""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from peewee import SqliteDatabase

if os.environ.get("GIBDD_STAGING") != "1":
    raise RuntimeError("This entry point is only for an explicitly enabled synthetic test environment")
directory = Path(os.environ["GIBDD_STAGING_DIR"]).resolve()
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
os.environ["MEDIA_STORAGE_DIR"] = str(directory / "media")

from app.db.database import database_proxy, create_tables, close_database
from app.main import create_app
from app.services.privacy import cleanup_personal_data

database_proxy.initialize(SqliteDatabase(directory / "test.sqlite3", pragmas={"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 5000}))
create_tables()
close_database()


@asynccontextmanager
async def lifespan(app):
    def cleanup():
        try:
            cleanup_personal_data()
        finally:
            close_database()
    async def loop():
        while True:
            await asyncio.to_thread(cleanup)
            await asyncio.sleep(30)
    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        close_database()


app = create_app(enable_lifespan=False)
app.router.lifespan_context = lifespan
