from fastapi import APIRouter

from finbot.adapters.http.schemas.common import ApiInfo


def meta_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["meta"])

    @router.get("", response_model=ApiInfo, summary="API metadata")
    async def api_info() -> ApiInfo:
        return ApiInfo()

    return router
