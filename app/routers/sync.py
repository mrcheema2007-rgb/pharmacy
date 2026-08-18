"""
The cloud mirror's push/pull/status API.

Flow:
  - The PC sync agent (source of truth = the pharmacy's Access database)
    pushes rows it has changed locally, and pulls rows other devices
    (e.g. the mobile app, writing while away from the pharmacy's network)
    pushed since it last checked - then applies those into Access via the
    local API's /sync/apply endpoint.
  - The mobile app, when it can't reach the local API directly (i.e. it's
    not on the pharmacy's WiFi), pushes queued writes here directly and
    reads through here as a fallback. Those writes only become "real" once
    the PC agent pulls them down and applies them to Access - see the
    LIMITATIONS section in README.md, particularly around auto-increment
    primary keys.
"""
import datetime
import json
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.auth import require_api_key
from app.config import get_settings
from app.db import get_pool
from app import materialize

router = APIRouter(prefix="/sync", tags=["Sync"], dependencies=[Depends(require_api_key)])

settings = get_settings()


class PushRecord(BaseModel):
    record_key: str
    data: dict
    row_hash: str


class PushRequest(BaseModel):
    device_id: str
    table_name: str
    records: List[PushRecord] = []
    deleted_keys: List[str] = []


class PushResponse(BaseModel):
    table_name: str
    upserted: int
    deleted: int


class PulledRecord(BaseModel):
    table_name: str
    record_key: str
    data: dict
    row_hash: str
    device_id: str
    updated_at: datetime.datetime
    deleted: bool


class PullResponse(BaseModel):
    records: List[PulledRecord]
    server_time: datetime.datetime


class TableStatus(BaseModel):
    table_name: str
    record_count: int
    last_updated_at: Optional[datetime.datetime]


def _to_json(data: dict) -> str:
    return json.dumps(data, default=str)


def _from_json(data) -> dict:
    if isinstance(data, str):
        return json.loads(data)
    return data


@router.post("/push", response_model=PushResponse, summary="Upsert changed rows from a device into the cloud mirror")
async def push(payload: PushRequest):
    if len(payload.records) > settings.MAX_RECORDS_PER_REQUEST:
        payload.records = payload.records[: settings.MAX_RECORDS_PER_REQUEST]

    pool = get_pool()
    upserted = 0
    deleted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for rec in payload.records:
                await conn.execute(
                    """
                    INSERT INTO synced_records (table_name, record_key, data, row_hash, device_id, updated_at, deleted)
                    VALUES ($1, $2, $3::jsonb, $4, $5, now(), false)
                    ON CONFLICT (table_name, record_key)
                    DO UPDATE SET data = $3::jsonb, row_hash = $4, device_id = $5, updated_at = now(), deleted = false
                    WHERE synced_records.row_hash IS DISTINCT FROM $4
                    """,
                    payload.table_name,
                    rec.record_key,
                    _to_json(rec.data),
                    rec.row_hash,
                    payload.device_id,
                )
                # Keep the typed mirror table (used by the cloud copy of
                # pharmacy_access_api) in step with the JSONB source of truth.
                await materialize.upsert_row(conn, payload.table_name, rec.data)
                upserted += 1

            for key in payload.deleted_keys:
                await conn.execute(
                    """
                    INSERT INTO synced_records (table_name, record_key, data, row_hash, device_id, updated_at, deleted)
                    VALUES ($1, $2, '{}'::jsonb, 'deleted', $3, now(), true)
                    ON CONFLICT (table_name, record_key)
                    DO UPDATE SET deleted = true, device_id = $3, updated_at = now()
                    """,
                    payload.table_name,
                    key,
                    payload.device_id,
                )
                await materialize.delete_row(conn, payload.table_name, key)
                deleted += 1

    return PushResponse(table_name=payload.table_name, upserted=upserted, deleted=deleted)


@router.get("/pull", response_model=PullResponse, summary="Get rows changed since a timestamp, excluding your own pushes")
async def pull(
    since: datetime.datetime = Query(..., description="ISO 8601 timestamp - only rows updated after this are returned"),
    table_name: Optional[str] = Query(None, description="Limit to one table; omit to pull all tables"),
    exclude_device_id: Optional[str] = Query(None, description="Don't echo back rows this same device just pushed"),
):
    pool = get_pool()
    conditions = ["updated_at > $1"]
    params: list = [since]

    if table_name:
        params.append(table_name)
        conditions.append(f"table_name = ${len(params)}")
    if exclude_device_id:
        params.append(exclude_device_id)
        conditions.append(f"device_id != ${len(params)}")

    where_sql = " AND ".join(conditions)
    sql = f"""
        SELECT table_name, record_key, data, row_hash, device_id, updated_at, deleted
        FROM synced_records
        WHERE {where_sql}
        ORDER BY updated_at ASC
        LIMIT {settings.MAX_RECORDS_PER_REQUEST}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    records = [
        PulledRecord(
            table_name=r["table_name"],
            record_key=r["record_key"],
            data=_from_json(r["data"]),
            row_hash=r["row_hash"],
            device_id=r["device_id"],
            updated_at=r["updated_at"],
            deleted=r["deleted"],
        )
        for r in rows
    ]
    return PullResponse(records=records, server_time=datetime.datetime.now(datetime.timezone.utc))


@router.get("/status", response_model=List[TableStatus], summary="Per-table record counts and last-updated timestamps")
async def status_():
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT table_name, COUNT(*) AS record_count, MAX(updated_at) AS last_updated_at
            FROM synced_records
            WHERE deleted = false
            GROUP BY table_name
            ORDER BY table_name
            """
        )
    return [
        TableStatus(table_name=r["table_name"], record_count=r["record_count"], last_updated_at=r["last_updated_at"])
        for r in rows
    ]
