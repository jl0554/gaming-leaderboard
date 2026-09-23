import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from redis.exceptions import AuthenticationError, ConnectionError, RedisError, ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from leaderboard.config import Settings
from leaderboard.main import create_app

SUBMISSION_KEY = "test-only-submission-key-observability-0123456789"
METRICS_KEY = "test-only-read-only-metrics-key-0123456789"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    application = create_app(
        Settings(_env_file=None, submission_api_key=SUBMISSION_KEY, metrics_api_key=METRICS_KEY)
    )
    with TestClient(application) as test_client:
        # Observability tests isolate instrumentation; existing integration tests use real Redis.
        monkeypatch.setattr(application.state.redis, "execute_command", AsyncMock(return_value=[]))
        monkeypatch.setattr(logging.getLogger("leaderboard"), "propagate", True)
        yield test_client


def events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "leaderboard.observability"
    ]


def scrape(client: TestClient) -> str:
    response = client.get("/metrics", headers={"X-Metrics-Key": METRICS_KEY})
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/plain")
    assert response.headers["Cache-Control"] == "no-store"
    return response.text


def test_generated_request_id_correlates_response_and_json_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": SUBMISSION_KEY})
    request_id = response.headers["X-Request-ID"]
    assert UUID(request_id).version == 4
    assert request_id != SUBMISSION_KEY
    event = next(event for event in events(caplog) if event["event"] == "http_request")
    assert event["request_id"] == request_id
    assert event["route"] == "/health/live"
    assert event["status"] == 200
    assert event["duration_ms"] >= 0
    assert SUBMISSION_KEY not in caplog.text


@pytest.mark.parametrize("path,status", [("/absent", 404), ("/games/g/leaderboard?limit=0", 422)])
def test_error_responses_also_have_request_ids(client: TestClient, path: str, status: int) -> None:
    response = client.get(path)
    assert response.status_code == status
    assert UUID(response.headers["X-Request-ID"]).version == 4


def test_logs_and_metrics_exclude_credentials_payloads_and_raw_identifiers(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    for game in ["private-game-one", "private-game-two"]:
        assert client.get(f"/games/{game}/leaderboard").status_code == 200
    response = client.post(
        "/games/private-game-three/scores?token=query-secret",
        headers={"X-API-Key": "header-secret", "X-Request-ID": "request-id-secret"},
        json={"user_id": "private-player", "score": "body-secret"},
    )
    assert response.status_code == 401
    client.request("PRIVATE_METHOD", "/private-path?token=query-secret")
    output = scrape(client) + caplog.text
    for secret in [
        SUBMISSION_KEY,
        METRICS_KEY,
        "private-game",
        "private-player",
        "query-secret",
        "header-secret",
        "request-id-secret",
        "body-secret",
        "private-path",
        "PRIVATE_METHOD",
    ]:
        assert secret not in output
    registry = client.app.state.observability.registry
    assert (
        registry.get_sample_value(
            "leaderboard_http_requests_total",
            {"method": "GET", "route": "/games/{game_id}/leaderboard", "status": "200"},
        )
        == 2
    )
    assert (
        registry.get_sample_value(
            "leaderboard_http_request_duration_seconds_count",
            {"method": "GET", "route": "/games/{game_id}/leaderboard"},
        )
        == 2
    )
    assert 'route="/metrics"' not in output


def test_metrics_are_disabled_without_a_key() -> None:
    application = create_app(
        Settings(_env_file=None, submission_api_key=SUBMISSION_KEY, metrics_api_key=None)
    )
    with TestClient(application) as client:
        assert client.get("/metrics").status_code == 404


def test_empty_metrics_environment_setting_disables_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEADERBOARD_METRICS_API_KEY", "")
    settings = Settings(_env_file=None, submission_api_key=SUBMISSION_KEY)
    assert settings.metrics_api_key is None


def test_monitoring_and_submission_keys_must_be_different() -> None:
    with pytest.raises(ValidationError, match="must be different") as error:
        Settings(_env_file=None, submission_api_key=SUBMISSION_KEY, metrics_api_key=SUBMISSION_KEY)
    assert SUBMISSION_KEY not in str(error.value)


@pytest.mark.parametrize("headers", [{}, {"X-Metrics-Key": "wrong"}, {"X-API-Key": SUBMISSION_KEY}])
def test_metrics_require_separate_credential(client: TestClient, headers: dict) -> None:
    response = client.get("/metrics", headers=headers)
    assert response.status_code == 401
    assert UUID(response.headers["X-Request-ID"]).version == 4


def test_monitoring_credential_does_not_authorize_writes(client: TestClient) -> None:
    response = client.post(
        "/games/g/scores",
        headers={"X-API-Key": METRICS_KEY},
        json={"user_id": "alice", "score": 100},
    )
    assert response.status_code == 401
    client.app.state.redis.execute_command.assert_not_called()


@pytest.mark.parametrize(
    "exception_type,category",
    [
        (AuthenticationError, "authentication"),
        (RedisTimeoutError, "timeout"),
        (ConnectionError, "connection"),
        (ResponseError, "response"),
        (RedisError, "other"),
    ],
)
@pytest.mark.parametrize(
    "path,operation", [("/games/g/leaderboard", "leaderboard"), ("/health/ready", "readiness")]
)
def test_redis_failure_categories_preserve_safe_responses(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    exception_type: type[RedisError],
    category: str,
    path: str,
    operation: str,
) -> None:
    client.app.state.redis.execute_command.side_effect = exception_type("redis-url-secret")
    response = client.get(path)
    assert response.status_code == 503
    failure = next(event for event in events(caplog) if event["event"] == "redis_failure")
    assert failure["category"] == category
    assert failure["request_id"] == response.headers["X-Request-ID"]
    assert (
        client.app.state.observability.registry.get_sample_value(
            "leaderboard_redis_errors_total", {"category": category, "operation": operation}
        )
        == 1
    )
    assert "redis-url-secret" not in caplog.text + response.text + scrape(client)


def test_unexpected_exception_is_safe_and_counted(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    client.app.state.redis.execute_command.side_effect = RuntimeError("internal-secret")
    response = client.get("/games/g/leaderboard")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    failure = next(event for event in events(caplog) if event["event"] == "unhandled_error")
    assert failure["request_id"] == response.headers["X-Request-ID"]
    assert failure["error_type"] == "RuntimeError"
    assert "internal-secret" not in caplog.text + response.text
    assert (
        client.app.state.observability.registry.get_sample_value(
            "leaderboard_http_requests_total",
            {"method": "GET", "route": "/games/{game_id}/leaderboard", "status": "500"},
        )
        == 1
    )


def test_concurrent_request_ids_and_counts_are_isolated(client: TestClient) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.get("/health/live"), range(24)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.headers["X-Request-ID"] for response in responses}) == 24
    assert (
        client.app.state.observability.registry.get_sample_value(
            "leaderboard_http_requests_total",
            {"method": "GET", "route": "/health/live", "status": "200"},
        )
        == 24
    )


def test_metrics_registries_do_not_leak_between_app_instances(client: TestClient) -> None:
    client.get("/health/live")
    other = create_app(
        Settings(_env_file=None, submission_api_key=SUBMISSION_KEY, metrics_api_key=METRICS_KEY)
    )
    with TestClient(other) as other_client:
        assert 'route="/health/live"' not in scrape(other_client)


@pytest.mark.parametrize("key", ["tiny-secret", "x" * 257, "x" * 32 + " ", "é" * 32])
def test_metrics_key_configuration_is_validated_without_echoing_secrets(key: str) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, submission_api_key=SUBMISSION_KEY, metrics_api_key=key)
    assert key not in str(error.value)
