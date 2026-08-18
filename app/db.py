"""
Database layer for the cloud sync service.

Design: rather than creating one Postgres table per Access table (which
would mean re-deriving 38 typed schemas and keeping them in lockstep with
the Access side), this service uses a single generic table:

    synced_records(table_name, record_key, data JSONB, row_hash,
                    device_id, updated_at, deleted)

keyed by (table_name, record_key) - record_key is the same pipe-joined
primary-key string the local Access API already uses in its URLs
(e.g. "1", or "BATCH01|1001|2026-12-31" for composite keys). This mirrors
the metadata-driven philosophy used throughout the rest of this project:
one implementation handles all 38 tables generically, instead of 38
hand-maintained Postgres schemas that could drift from Access over time.
"""
import asyncpg

from app.config import get_settings

settings = get_settings()

_pool: asyncpg.Pool | None = None

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS synced_records (
    table_name   TEXT        NOT NULL,
    record_key   TEXT        NOT NULL,
    data         JSONB       NOT NULL,
    row_hash     TEXT        NOT NULL,
    device_id    TEXT        NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted      BOOLEAN     NOT NULL DEFAULT false,
    PRIMARY KEY (table_name, record_key)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_synced_records_table_updated
    ON synced_records (table_name, updated_at);
"""


def _normalize_dsn(dsn: str) -> str:
    # Some providers (Heroku-style, older Railway templates) hand out
    # "postgres://" - asyncpg wants "postgresql://".
    if dsn.startswith("postgres://"):
        return "postgresql://" + dsn[len("postgres://") :]
    return dsn


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(_normalize_dsn(settings.DATABASE_URL), min_size=1, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute(_CREATE_TABLE_SQL)
        await conn.execute(_CREATE_INDEX_SQL)
        # Typed per-table mirror (one real Postgres table per Access table,
        # same name, quoted) used by a cloud-deployed copy of
        # pharmacy_access_api - see app/materialize.py.
        from app.materialize import create_all_tables

        await create_all_tables(conn)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized - init_pool() must run at app startup.")
    return _pool


async def test_connection() -> bool:
    try:
        async with get_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False
