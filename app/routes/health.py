from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(prefix="", tags=["health"])


@router.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "service": settings.app_name,
        "environment": settings.app_env,
        "demo_mode": settings.demo_mode,
        "sources": settings.enabled_sources,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
