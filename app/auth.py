"""
API-key protection for the cloud sync service.

This is NOT the user-login/RBAC system that was deliberately removed from
the rest of this project (the source Access database has no Users table,
so this project has no concept of a "user"). This is a much narrower
thing: a single shared secret that proves a request came from your own PC
sync agent or your own mobile app, since this service - unlike the local
Access-backed API - is reachable by anyone on the internet who finds its
Railway URL. Without this, anyone could push arbitrary data into your
cloud mirror or read it.

Every device (the PC agent, the mobile app) is configured with the same
SYNC_API_KEY value that you set in Railway's environment variables.
"""
from fastapi import Header, HTTPException, status

from app.config import get_settings

settings = get_settings()


async def require_api_key(x_sync_api_key: str = Header(..., alias="X-Sync-Api-Key")) -> None:
    if x_sync_api_key != settings.SYNC_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-Sync-Api-Key header.")
