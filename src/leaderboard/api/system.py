from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from redis.exceptions import RedisError

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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis is unavailable",
        ) from exc
    return HealthResponse(status="ok", service=settings.app_name)
