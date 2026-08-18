"""
Configuration for the cloud sync service.

On Railway, DATABASE_URL is injected automatically once a PostgreSQL
plugin is attached to this service (see README.md for the exact steps).
SYNC_API_KEY is something *you* set as a Railway environment variable -
it's the shared secret the PC sync agent and the mobile app send back to
prove they're allowed to write to your data, since this service (unlike
the local Access-backed API) is reachable from the public internet.
"""
import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "Pharmacy Cloud Sync Service"
    APP_VERSION: str = "1.0.0"

    # Railway injects this automatically when you attach a PostgreSQL plugin
    # to this service. Locally, put a real Postgres URL in .env to test.
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/pharmacy_sync"

    # Shared secret - set this yourself in Railway's Variables tab, and use
    # the same value in the PC sync agent's .env and the mobile app's config.
    SYNC_API_KEY: str = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"

    # Railway sets PORT automatically at runtime; default only matters for local dev.
    PORT: int = int(os.environ.get("PORT", 8080))

    CORS_ALLOW_ORIGINS: list[str] = ["*"]

    # Safety cap so a single push/pull request can't blow up memory/time.
    MAX_RECORDS_PER_REQUEST: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
