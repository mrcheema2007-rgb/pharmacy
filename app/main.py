"""
Cloud sync service entrypoint.

This is a small, separate FastAPI app from the main pharmacy_access_api -
it does NOT talk to Access/pyodbc at all (Railway can't reach a Windows
PC's local .accdb file, and shouldn't try to). It only stores/serves a
generic JSONB mirror of whatever the PC sync agent pushes to it. See
README.md for what this is (and isn't), and exact Railway deployment
steps including provisioning the Postgres database.

Run locally with:
    uvicorn app.main:app --reload --port 8080
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import close_pool, init_pool, test_connection
from app.routers import sync as sync_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("cloud_sync")

settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Generic cloud mirror for the pharmacy's Access database. Stores "
        "whatever the PC sync agent pushes to it (as JSONB, one row per "
        "Access record) and serves it back for pull. This service holds no "
        "business logic of its own and is not a replacement for the local "
        "Access-backed API - see README.md."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sync_router.router)


@app.on_event("startup")
async def on_startup():
    logger.info("Starting %s v%s", settings.APP_NAME, settings.APP_VERSION)
    await init_pool()
    if not await test_connection():
        logger.error("Could not connect to Postgres. Check DATABASE_URL.")
    else:
        logger.info("Connected to Postgres OK.")


@app.on_event("shutdown")
async def on_shutdown():
    await close_pool()


@app.get("/health", tags=["Health"], summary="Liveness check")
async def health():
    return {"status": "ok"}


@app.get("/health/db", tags=["Health"], summary="Database connectivity check")
async def health_db():
    ok = await test_connection()
    return {"database": "reachable" if ok else "unreachable"}


@app.get("/", tags=["Root"], summary="Service info")
async def root():
    return {"name": settings.APP_NAME, "version": settings.APP_VERSION, "docs": "/docs"}
