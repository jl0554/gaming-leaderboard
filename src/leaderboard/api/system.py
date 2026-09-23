from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from redis.exceptions import RedisError

from leaderboard.security import require_metrics_key

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    service: str


@router.get("/health/live", response_model=HealthResponse)
async def liveness(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    return HealthResponse(status="ok", service=settings.app_name)


@router.get("/health/ready", response_model=HealthResponse)
async def readiness(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    try:
        await request.app.state.redis.ping()
    except RedisError as exc:
        request.app.state.observability.record_redis_error(
            request.scope, exc, operation="readiness"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis is unavailable",
        ) from exc
    return HealthResponse(status="ok", service=settings.app_name)


@router.get("/metrics", dependencies=[Depends(require_metrics_key)], include_in_schema=False)
async def metrics(request: Request) -> Response:
    return Response(
        content=generate_latest(request.app.state.observability.registry),
        headers={"Content-Type": CONTENT_TYPE_LATEST, "Cache-Control": "no-store"},
    )
