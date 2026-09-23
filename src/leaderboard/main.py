import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from leaderboard.api.leaderboard import router as leaderboard_router
from leaderboard.api.system import router as system_router
from leaderboard.config import Settings, get_settings
from leaderboard.storage import LeaderboardStore, PlayerNotRankedError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_connect_timeout,
        retry=Retry(NoBackoff(), 0),
    )
    app.state.redis = redis
    app.state.leaderboard = LeaderboardStore(redis, settings.redis_key_prefix)
    try:
        yield
    finally:
        await redis.aclose()


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Raw inputs can contain infinities (e.g. JSON 1e999) or sensitive values.
    # Keep the useful validation details without echoing input or context.
    errors = [{field: error[field] for field in ("type", "loc", "msg")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


async def player_not_ranked_handler(request: Request, exc: PlayerNotRankedError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "PLAYER_NOT_RANKED",
                "message": "Player has not submitted a score for this game.",
            }
        },
    )


async def redis_error_handler(request: Request, exc: RedisError) -> JSONResponse:
    # Log the error type only: connection exceptions can contain credentials.
    logger.warning("Leaderboard storage failure: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "REDIS_UNAVAILABLE",
                "message": "Leaderboard storage is temporarily unavailable.",
            }
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Real-time, multi-game leaderboard API.",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.add_exception_handler(RequestValidationError, request_validation_error_handler)
    application.add_exception_handler(PlayerNotRankedError, player_not_ranked_handler)
    application.add_exception_handler(RedisError, redis_error_handler)
    application.include_router(system_router)
    application.include_router(leaderboard_router)
    return application


app = create_app()
