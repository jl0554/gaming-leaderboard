"""Bounded metrics and allowlisted JSON logs; never log raw requests or exceptions."""

import json
import logging
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from prometheus_client import CollectorRegistry, Counter, Histogram
from redis.exceptions import AuthenticationError, ConnectionError, RedisError, ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("leaderboard.observability")
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"})


def configure_logging() -> None:
    """Configure only our logger, leaving the embedding application's root logger alone."""
    application_logger = logging.getLogger("leaderboard")
    if not application_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        application_logger.addHandler(handler)
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False


def route_template(scope: Scope) -> str:
    # Raw URLs, game IDs and player IDs would leak data and create unbounded series.
    return getattr(scope.get("route"), "path", "unmatched")


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    logger.log(
        level,
        json.dumps(
            {"timestamp": datetime.now(UTC).isoformat(), "event": event, **fields},
            separators=(",", ":"),
        ),
    )


def redis_error_category(exc: RedisError) -> str:
    # AuthenticationError is a ConnectionError subclass: check it first.
    if isinstance(exc, AuthenticationError):
        return "authentication"
    if isinstance(exc, RedisTimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "connection"
    if isinstance(exc, ResponseError):
        return "response"
    return "other"


class Observability:
    def __init__(self) -> None:
        # Per-application registry avoids cross-test contamination/duplicate registration.
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "leaderboard_http_requests_total",
            "Completed HTTP requests, excluding metrics scrapes.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "leaderboard_http_request_duration_seconds",
            "HTTP request duration, excluding metrics scrapes.",
            ["method", "route"],
            registry=self.registry,
        )
        self.redis_errors = Counter(
            "leaderboard_redis_errors_total",
            "Redis failures by bounded category and operation.",
            ["category", "operation"],
            registry=self.registry,
        )

    def record_redis_error(self, scope: Scope, exc: RedisError, *, operation: str) -> None:
        category = redis_error_category(exc)
        self.redis_errors.labels(category, operation).inc()
        log_event(
            "redis_failure",
            level=logging.WARNING if category in {"connection", "timeout"} else logging.ERROR,
            request_id=scope.get("state", {}).get("request_id"),
            route=route_template(scope),
            category=category,
            operation=operation,
            error_type=type(exc).__name__,
        )


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, observability: Observability) -> None:
        self.app = app
        self.observability = observability

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Always generate locally: an untrusted incoming ID could contain a credential.
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            log_event(
                "unhandled_error",
                level=logging.ERROR,
                request_id=request_id,
                route=route_template(scope),
                error_type=type(exc).__name__,
            )
            # Current routes do not stream; after headers are sent we cannot replace them.
            if response_started:
                raise
            response = JSONResponse(
                status_code=500,
                content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error."}},
            )
            await response(scope, receive, send_with_id)
        finally:
            route = route_template(scope)
            if route != "/metrics":
                method = scope["method"] if scope["method"] in HTTP_METHODS else "OTHER"
                elapsed = perf_counter() - started
                self.observability.requests.labels(method, route, str(status)).inc()
                self.observability.duration.labels(method, route).observe(elapsed)
                log_event(
                    "http_request",
                    request_id=request_id,
                    method=method,
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 3),
                )
