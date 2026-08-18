import asyncio

from fastapi import APIRouter

from finbot.adapters.http.errors import HttpApiError, HttpErrorCode
from finbot.adapters.http.ports import ReadinessProbe
from finbot.adapters.http.schemas.common import HealthStatus

READINESS_TIMEOUT_SECONDS = 2.0


def health_router(readiness: ReadinessProbe) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get(
        "/health/live",
        response_model=HealthStatus,
        summary="Process liveness",
    )
    async def live() -> HealthStatus:
        return HealthStatus()

    @router.get(
        "/health/ready",
        response_model=HealthStatus,
        summary="Database and migration readiness",
        responses={503: {"description": "Service is not ready"}},
    )
    async def ready() -> HealthStatus:
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                await readiness.check()
        except Exception:
            raise HttpApiError(
                status_code=503,
                code=HttpErrorCode.READINESS_FAILED,
            ) from None
        return HealthStatus()

    return router
