"""
Schema metadata loader for the cloud sync service.

This is a copy of the same `schema_metadata.json` used by
`pharmacy_access_api` (generated from the Access documentation). It's
duplicated here - rather than shared via a package - because these are
two independently deployable services; if you rename/add a column in
Access, regenerate this file the same way you'd regenerate the one in
pharmacy_access_api and copy it across. See app/materialize.py for why
this service needs it at all: it uses these column definitions to create
real typed Postgres tables (in addition to the generic `synced_records`
JSONB mirror) so that a cloud-deployed copy of pharmacy_access_api can run
genuine SQL (joins, aggregates, sorting) against real columns instead of
opaque JSON blobs.
"""
import json
from pathlib import Path
from typing import Any, Dict, List

_METADATA_PATH = Path(__file__).parent / "schema_metadata.json"

with open(_METADATA_PATH, "r", encoding="utf-8") as f:
    TABLES: Dict[str, Dict[str, Any]] = json.load(f)

TABLE_NAMES: List[str] = sorted(TABLES.keys())


def get_table(table_name: str) -> Dict[str, Any]:
    if table_name not in TABLES:
        raise KeyError(f"Unknown table '{table_name}'")
    return TABLES[table_name]


def get_pk_fields(table_name: str) -> List[str]:
    return TABLES[table_name]["pk_fields"]


def get_columns(table_name: str) -> List[Dict[str, Any]]:
    return [c for c in TABLES[table_name]["columns"] if c.get("exposed", True)]


def quote(identifier: str) -> str:
    """Double-quote a Postgres identifier so exact Access casing/spaces/
    slashes (e.g. 'Basic Pay', 'A/c_Number') survive as real column names."""
    return '"' + identifier.replace('"', '""') + '"'
