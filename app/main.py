from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.routes.health import router as health_router
from app.routes.legacy_compat import router as legacy_router
from app.routes.matches import router as matches_router
from app.routes.predict import router as predict_router
from app.services.aggregator import MatchAggregator
from app.services.settlement import start_scheduler, stop_scheduler


def create_app() -> FastAPI:
    settings = get_settings()
    aggregator = MatchAggregator(settings=settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # startup
        start_scheduler()
        yield
        # shutdown
        stop_scheduler()

    app = FastAPI(
        title="Football Intelligence API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(health_router)
    app.include_router(matches_router)
    app.include_router(predict_router)
    app.include_router(legacy_router)

    @app.get("/")
    async def root() -> dict[str, object]:
        return {
            "service": settings.app_name,
            "version": app.version,
            "docs_url": app.docs_url,
            "enabled_sources": settings.enabled_sources,
        }

    @app.get("/sources")
    async def get_sources() -> object:
        return aggregator.describe_sources()

    @app.get("/odds/{match_id}")
    async def get_match_odds(match_id: str) -> object:
        snapshot = await aggregator.get_match_snapshot(match_id)
        if not snapshot.odds:
            raise HTTPException(status_code=404, detail="Odds not found for this match")
        return snapshot.odds

    return app


app = create_app()
