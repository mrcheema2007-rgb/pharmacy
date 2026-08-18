"""
Typed table materialization.

`synced_records` (see app/db.py) is the source of truth for sync bookkeeping
(row hashes, device ids, conflict-free upserts) and is deliberately generic
- one JSONB blob per row, so this service doesn't need 38 hand-maintained
Postgres schemas.

But a *cloud-deployed copy of pharmacy_access_api* needs to run real SQL:
joins (Stock + Pro), aggregates (SUM/COUNT), ORDER BY, LIKE search - the
same queries the local Access-backed copy runs. That's not possible against
an opaque JSONB column. So this module keeps a second representation next
to the JSONB mirror: one real Postgres table per Access table, with real
typed columns, generated from the same schema_metadata.json that
pharmacy_access_api uses to build its own SQL. Every push into
`synced_records` also upserts (or deletes) the matching row here, in the
same transaction, so the two are always consistent.

Binary ("bytes"/OLE) columns are intentionally excluded from the typed
tables - they're never filtered, sorted, or joined on, and base64-in-JSON
round-tripping into BYTEA is more risk than it's worth. They remain
available in the JSONB blob for any consumer that reads `synced_records`
directly.
"""
import logging
from typing import Any, Dict, List

import asyncpg

from app.schema_metadata import TABLE_NAMES, get_columns, get_pk_fields, get_table, quote

logger = logging.getLogger("app.materialize")

_PG_TYPES = {
    "int": "BIGINT",
    "str": "TEXT",
    "float": "DOUBLE PRECISION",
    "bool": "BOOLEAN",
    "datetime": "TIMESTAMPTZ",
}
_CAST = {
    "int": "bigint",
    "str": "text",
    "float": "double precision",
    "bool": "boolean",
    "datetime": "timestamptz",
}


def _typed_columns(table_name: str) -> List[Dict[str, Any]]:
    """Materializable columns for a table - every exposed column except
    binary/OLE ones, which have no typed-table representation."""
    return [c for c in get_columns(table_name) if c["py_type"] != "bytes"]


async def create_all_tables(conn: asyncpg.Connection) -> None:
    for table_name in TABLE_NAMES:
        cols = _typed_columns(table_name)
        if not cols:
            continue
        pk_fields = [f for f in get_pk_fields(table_name) if f in {c["access_name"] for c in cols}]

        col_defs = []
        for c in cols:
            pg_type = _PG_TYPES.get(c["py_type"], "TEXT")
            col_defs.append(f"{quote(c['access_name'])} {pg_type}")

        pk_sql = f", PRIMARY KEY ({', '.join(quote(f) for f in pk_fields)})" if pk_fields else ""
        ddl = f'CREATE TABLE IF NOT EXISTS {quote(table_name)} ({", ".join(col_defs)}{pk_sql});'
        await conn.execute(ddl)
    logger.info("Materialized %d typed tables alongside synced_records", len(TABLE_NAMES))


def _record_key_for(table_name: str, data: Dict[str, Any]) -> str:
    return "|".join(str(data.get(f, "")) for f in get_pk_fields(table_name))


async def upsert_row(conn: asyncpg.Connection, table_name: str, data: Dict[str, Any]) -> None:
    """Upsert one row's exposed, non-binary columns into the typed table
    that mirrors this Access table. Silently skipped for unknown tables
    (e.g. a table not present in schema_metadata.json) or rows missing
    required primary key values."""
    if table_name not in TABLE_NAMES:
        return
    cols = _typed_columns(table_name)
    if not cols:
        return
    pk_fields = set(get_pk_fields(table_name))

    names = [c["access_name"] for c in cols]
    values = [data.get(n) for n in names]
    placeholders = [f"${i + 1}::{_CAST.get(c['py_type'], 'text')}" for i, c in enumerate(cols)]

    update_cols = [n for n in names if n not in pk_fields]
    if update_cols:
        set_sql = ", ".join(f"{quote(n)} = EXCLUDED.{quote(n)}" for n in update_cols)
        conflict_sql = f"ON CONFLICT ({', '.join(quote(f) for f in pk_fields if f in names)}) DO UPDATE SET {set_sql}"
    else:
        conflict_sql = "ON CONFLICT DO NOTHING"

    sql = (
        f'INSERT INTO {quote(table_name)} ({", ".join(quote(n) for n in names)}) '
        f"VALUES ({', '.join(placeholders)}) {conflict_sql};"
    )
    try:
        await conn.execute(sql, *values)
    except Exception as exc:  # noqa: BLE001
        # Never let a materialization failure block the JSONB sync write,
        # which remains the source of truth for the sync protocol itself.
        logger.error("Materialize upsert failed for %s: %s", table_name, exc)


async def delete_row(conn: asyncpg.Connection, table_name: str, record_key: str) -> None:
    if table_name not in TABLE_NAMES:
        return
    pk_fields = get_pk_fields(table_name)
    pk_values = record_key.split("|")
    if len(pk_values) != len(pk_fields):
        return
    col_map = {c["access_name"]: c for c in get_columns(table_name)}
    where_sql = " AND ".join(
        f"{quote(f)} = ${i + 1}::{_CAST.get(col_map.get(f, {}).get('py_type'), 'text')}"
        for i, f in enumerate(pk_fields)
    )
    sql = f'DELETE FROM {quote(table_name)} WHERE {where_sql};'
    try:
        await conn.execute(sql, *pk_values)
    except Exception as exc:  # noqa: BLE001
        logger.error("Materialize delete failed for %s: %s", table_name, exc)
