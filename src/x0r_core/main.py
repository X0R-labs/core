from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.v1 import router
from .config import get_settings
from .db import dispose_db, init_db
from .schemas import HealthOut

VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await dispose_db()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="X0R Core",
        version=VERSION,
        summary="Self-hosted AI agent backend",
        description=(
            "Ingest your documents, run tool-calling agents over them, and meter every "
            "token. Provider-agnostic and designed to be owned, not rented."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.get("/health", response_model=HealthOut, tags=["ops"])
    async def health() -> HealthOut:
        return HealthOut(
            status="ok", version=VERSION, provider=settings.provider, env=settings.env
        )

    @app.get("/", tags=["ops"])
    async def root() -> dict:
        return {
            "name": "X0R Core",
            "version": VERSION,
            "docs": "/docs",
            "health": "/health",
            "api": "/v1",
        }

    return app


app = create_app()
