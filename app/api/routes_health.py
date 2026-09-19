"""Health and pipeline metadata endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.core.responses import ok
from app.core.security import require_admin
from app.pipelines.stages import STAGE_ORDER
from app.services.ai_providers import providers_status

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    settings = get_settings()
    return ok(
        {
            "status": "healthy",
            "env": settings.env,
            "mock_mode": settings.mock_mode,
        }
    )


@router.get("/pipeline/info")
def pipeline_info(_user=Depends(require_admin)):
    stages = [
        {
            "name": cls.name,
            "requires": list(cls.requires),
            "provides": list(cls.provides),
        }
        for cls in STAGE_ORDER
    ]
    return ok(
        {
            "pipeline": {
                "name": "document_screening",
                "stages": stages,
                "providers": providers_status(),
            }
        }
    )
