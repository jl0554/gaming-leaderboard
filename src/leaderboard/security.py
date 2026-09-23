"""Shared credential for trusted score-submitting game servers."""

from secrets import compare_digest
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

submission_key_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ScoreSubmissionKey",
    description="Trusted game-server credential required to submit scores.",
    auto_error=False,
)

metrics_key_header = APIKeyHeader(
    name="X-Metrics-Key",
    scheme_name="MetricsKey",
    description="Read-only monitoring credential; distinct from the score submission key.",
    auto_error=False,
)


async def require_metrics_key(
    request: Request,
    supplied_key: Annotated[str | None, Security(metrics_key_header)],
) -> None:
    configured_key = request.app.state.settings.metrics_api_key
    if configured_key is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if supplied_key is None or not compare_digest(
        supplied_key.encode("utf-8"), configured_key.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing metrics key",
            headers={"WWW-Authenticate": "APIKey"},
        )


async def require_submission_key(
    request: Request,
    supplied_key: Annotated[str | None, Security(submission_key_header)],
) -> None:
    configured_key = request.app.state.settings.submission_api_key
    if (
        configured_key is None
        or supplied_key is None
        or not compare_digest(
            supplied_key.encode("utf-8"), configured_key.get_secret_value().encode("utf-8")
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "APIKey"},
        )
