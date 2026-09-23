"""HTTP integration coverage using an isolated, real Redis server."""

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from leaderboard.config import Settings
from leaderboard.main import create_app

# Deliberately public fixture data; never a production credential.
FAKE_API_KEY = "test-only-submission-key-not-a-real-secret-0123456789"


@pytest.fixture(scope="session")
def redis_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Launch a dedicated process: never reuse or clear the developer's database."""
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.fail("Integration tests require redis-server on PATH; install Redis and rerun.")

    directory = tmp_path_factory.mktemp("leaderboard-redis")
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    url = f"redis://127.0.0.1:{port}/0"
    log_path: Path = directory / "redis.log"
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            [
                executable,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(directory),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        probe = Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Redis failed to start: {log_path.read_text()}")
                try:
                    if probe.ping():
                        break
                except RedisError:
                    time.sleep(0.05)
            else:
                pytest.fail(f"Redis did not become ready: {log_path.read_text()}")
            yield url
        finally:
            probe.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.fixture
def client(redis_url: str) -> Iterator[TestClient]:
    prefix = f"test-leaderboard-{uuid4().hex}"
    application = create_app(
        Settings(
            _env_file=None,
            submission_api_key=FAKE_API_KEY,
            redis_url=redis_url,
            redis_key_prefix=prefix,
            redis_socket_timeout=0.5,
            redis_connect_timeout=0.5,
        )
    )
    try:
        with TestClient(application, headers={"X-API-Key": FAKE_API_KEY}) as test_client:
            yield test_client
    finally:
        with Redis.from_url(redis_url, socket_timeout=1) as cleanup:
            # Only this fixture's keys; never FLUSHDB or broad shared-key deletion.
            owned_keys = list(cleanup.scan_iter(match=f"{prefix}:*"))
            if owned_keys:
                cleanup.delete(*owned_keys)


def entry(user_id: str, score: int, rank: int) -> dict:
    return {"user_id": user_id, "score": score, "rank": rank}


def submit(client: TestClient, user_id: str, score: int, game: str = "chess") -> dict:
    response = client.post(f"/games/{game}/scores", json={"user_id": user_id, "score": score})
    assert response.status_code == 200, response.text
    return response.json()


def seed(client: TestClient, players: list[tuple[str, int]], game: str = "chess") -> None:
    for user_id, score in players:
        submit(client, user_id, score, game)


def test_score_updates_preserve_best_and_are_repeatable(client: TestClient) -> None:
    for score, best, updated in [
        (0, 0, True),
        (100, 100, True),
        (80, 100, False),
        (100, 100, False),
        (100, 100, False),
        (150, 150, True),
    ]:
        assert submit(client, "alice", score) == {
            "game_id": "chess",
            "user_id": "alice",
            "score": best,
            "rank": 1,
            "updated": updated,
        }
    response = client.get("/games/chess/leaderboard")
    assert response.json() == {"game_id": "chess", "entries": [entry("alice", 150, 1)]}


def test_submit_returns_shared_rank_and_rank_changes(client: TestClient) -> None:
    assert submit(client, "alice", 100)["rank"] == 1
    assert submit(client, "bob", 100)["rank"] == 1
    assert submit(client, "carol", 90)["rank"] == 3
    assert submit(client, "carol", 110)["rank"] == 1
    lower_submission = submit(client, "alice", 20)
    assert lower_submission["score"] == 100
    assert lower_submission["rank"] == 2
    assert lower_submission["updated"] is False


def test_top_has_shared_ranks_and_ascending_id_order(client: TestClient) -> None:
    seed(client, [("carol", 90), ("bob", 100), ("alice", 100)])
    response = client.get("/games/chess/leaderboard")
    assert response.status_code == 200
    assert response.json() == {
        "game_id": "chess",
        "entries": [entry("alice", 100, 1), entry("bob", 100, 1), entry("carol", 90, 3)],
    }


def test_tied_cutoff_counts_players_and_is_stable(client: TestClient) -> None:
    seed(client, [("b", 100), ("a", 100), ("B", 100), ("A", 100), ("z", 90)])
    for _ in range(3):
        response = client.get("/games/chess/leaderboard", params={"limit": 3})
        assert response.status_code == 200
        assert response.json()["entries"] == [
            entry("A", 100, 1),
            entry("B", 100, 1),
            entry("a", 100, 1),
        ]


def test_top_default_limit_and_limit_larger_than_population(client: TestClient) -> None:
    seed(client, [(f"p{i:02}", 100 - i) for i in range(12)])
    assert len(client.get("/games/chess/leaderboard").json()["entries"]) == 10
    response = client.get("/games/chess/leaderboard", params={"limit": 100})
    assert response.status_code == 200
    assert response.json()["entries"] == [entry(f"p{i:02}", 100 - i, i + 1) for i in range(12)]
    assert len(client.get("/games/chess/leaderboard", params={"limit": 1}).json()["entries"]) == 1


def test_empty_leaderboard(client: TestClient) -> None:
    response = client.get("/games/empty/leaderboard")
    assert response.status_code == 200
    assert response.json() == {"game_id": "empty", "entries": []}


@pytest.mark.parametrize("game", ["empty", "chess"])
def test_unranked_player_has_clear_404(client: TestClient, game: str) -> None:
    submit(client, "alice", 100)
    response = client.get(f"/games/{game}/players/nobody/context")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLAYER_NOT_RANKED"
    assert response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("user_id", "player", "above", "below"),
    [
        ("alice", entry("alice", 100, 1), [], [entry("bob", 100, 1)]),
        ("bob", entry("bob", 100, 1), [entry("alice", 100, 1)], [entry("carol", 90, 3)]),
        ("carol", entry("carol", 90, 3), [entry("bob", 100, 1)], []),
    ],
)
def test_context_edges_and_tied_neighbors(
    client: TestClient, user_id: str, player: dict, above: list, below: list
) -> None:
    seed(client, [("bob", 100), ("alice", 100), ("carol", 90)])
    response = client.get(f"/games/chess/players/{user_id}/context")
    assert response.status_code == 200
    assert response.json() == {"game_id": "chess", "player": player, "above": above, "below": below}


@pytest.mark.parametrize("radius", [0, 1, 10])
def test_only_player_has_no_neighbors(client: TestClient, radius: int) -> None:
    submit(client, "solo", 50)
    response = client.get("/games/chess/players/solo/context", params={"radius": radius})
    assert response.status_code == 200
    assert response.json() == {
        "game_id": "chess",
        "player": entry("solo", 50, 1),
        "above": [],
        "below": [],
    }


def test_context_radius_zero_excludes_existing_neighbors(client: TestClient) -> None:
    seed(client, [("alice", 100), ("bob", 100), ("carol", 90)])
    response = client.get("/games/chess/players/bob/context", params={"radius": 0})
    assert response.status_code == 200
    assert response.json() == {
        "game_id": "chess",
        "player": entry("bob", 100, 1),
        "above": [],
        "below": [],
    }


@pytest.mark.parametrize(
    ("user_id", "above", "below"),
    [
        ("d", [entry("c", 100, 2)], [entry("e", 100, 2)]),
        ("f", [entry("e", 100, 2)], [entry("z", 90, 8)]),
    ],
)
def test_context_mid_tie_uses_global_ranks(
    client: TestClient, user_id: str, above: list, below: list
) -> None:
    seed(client, [("leader", 200), *[(letter, 100) for letter in "abcdef"], ("z", 90)])
    response = client.get(f"/games/chess/players/{user_id}/context")
    assert response.status_code == 200
    assert response.json() == {
        "game_id": "chess",
        "player": entry(user_id, 100, 2),
        "above": above,
        "below": below,
    }


def test_multiple_neighbors_keep_display_order_and_clamp_boundaries(client: TestClient) -> None:
    seed(client, [("a", 100), ("b", 100), ("c", 90), ("d", 80), ("e", 80)])
    response = client.get("/games/chess/players/c/context", params={"radius": 10})
    assert response.status_code == 200
    assert response.json() == {
        "game_id": "chess",
        "player": entry("c", 90, 3),
        "above": [entry("a", 100, 1), entry("b", 100, 1)],
        "below": [entry("d", 80, 4), entry("e", 80, 4)],
    }


def test_games_are_independent(client: TestClient) -> None:
    submit(client, "alice", 100, "chess")
    submit(client, "alice", 20, "racing")
    submit(client, "bob", 30, "racing")
    assert client.get("/games/chess/leaderboard").json()["entries"] == [entry("alice", 100, 1)]
    assert client.get("/games/racing/leaderboard").json()["entries"] == [
        entry("bob", 30, 1),
        entry("alice", 20, 2),
    ]
    assert client.get("/games/chess/players/bob/context").status_code == 404
    assert client.get("/games/racing/players/alice/context").json()["player"] == entry(
        "alice", 20, 2
    )


def test_simultaneous_submissions_preserve_maximum(client: TestClient) -> None:
    scores = [0, 99, 8, 1000, 300, 27, 999, 1000, 16, 100, 1, 400] * 2
    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda score: submit(client, "alice", score), scores))
    for score, response in zip(scores, responses, strict=True):
        assert response["score"] >= score
        assert response["rank"] == 1
    assert client.get("/games/chess/leaderboard").json()["entries"] == [entry("alice", 1000, 1)]


@pytest.mark.parametrize("score", [0, 1_000_000_000])
def test_score_boundaries_are_valid(client: TestClient, score: int) -> None:
    assert submit(client, "alice", score)["score"] == score


@pytest.mark.parametrize("score", [True, False, "100", 1.0, 1.5, -1, 1_000_000_001, None, [], {}])
def test_scores_are_strict_bounded_integers(client: TestClient, score: object) -> None:
    response = client.post("/games/chess/scores", json={"user_id": "alice", "score": score})
    assert response.status_code == 422
    assert client.get("/games/chess/leaderboard").json()["entries"] == []


@pytest.mark.parametrize("body", [{}, {"user_id": "alice"}, {"score": 100}, [], None])
def test_missing_or_malformed_score_body(client: TestClient, body: object) -> None:
    response = client.post("/games/chess/scores", json=body)
    assert response.status_code == 422


def test_malformed_json(client: TestClient) -> None:
    response = client.post(
        "/games/chess/scores", content='{"score":', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("identifier", ["a", "A0_-", "x" * 64])
def test_valid_identifier_boundaries(client: TestClient, identifier: str) -> None:
    assert submit(client, identifier, 1, identifier)["user_id"] == identifier
    response = client.get(f"/games/{identifier}/players/{identifier}/context")
    assert response.status_code == 200
    assert response.json()["player"] == entry(identifier, 1, 1)


@pytest.mark.parametrize("user_id", ["", "x" * 65, "alice smith", "álîce", "a:b", "a.b", 123, None])
def test_invalid_player_body_ids(client: TestClient, user_id: object) -> None:
    response = client.post("/games/chess/scores", json={"user_id": user_id, "score": 100})
    assert response.status_code == 422


@pytest.mark.parametrize("identifier", ["x" * 65, "bad id", "álîce", "a:b", "a.b"])
def test_invalid_game_ids_on_all_endpoints(client: TestClient, identifier: str) -> None:
    assert (
        client.post(
            f"/games/{identifier}/scores", json={"user_id": "alice", "score": 100}
        ).status_code
        == 422
    )
    assert client.get(f"/games/{identifier}/leaderboard").status_code == 422
    assert client.get(f"/games/{identifier}/players/alice/context").status_code == 422


@pytest.mark.parametrize("identifier", ["x" * 65, "bad id", "álîce", "a:b", "a.b"])
def test_invalid_context_player_ids(client: TestClient, identifier: str) -> None:
    assert client.get(f"/games/chess/players/{identifier}/context").status_code == 422


@pytest.mark.parametrize("limit", [0, -1, 101, "1.5", "many", "true", ""])
def test_invalid_top_limits(client: TestClient, limit: object) -> None:
    assert client.get("/games/chess/leaderboard", params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("radius", [-1, 11, "1.5", "many", "true", ""])
def test_invalid_context_radius(client: TestClient, radius: object) -> None:
    assert (
        client.get("/games/chess/players/alice/context", params={"radius": radius}).status_code
        == 422
    )


@pytest.mark.parametrize("exception_type", [RedisConnectionError, RedisTimeoutError])
@pytest.mark.parametrize("operation", ["submit", "top", "context"])
def test_redis_failure_returns_safe_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, exception_type: type, operation: str
) -> None:
    monkeypatch.setattr(
        client.app.state.redis,
        "execute_command",
        AsyncMock(side_effect=exception_type("private-redis-credential-do-not-expose")),
    )
    if operation == "submit":
        response = client.post("/games/chess/scores", json={"user_id": "alice", "score": 100})
    elif operation == "top":
        response = client.get("/games/chess/leaderboard")
    else:
        response = client.get("/games/chess/players/alice/context")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "REDIS_UNAVAILABLE"
    assert response.json()["error"]["message"]
    assert "private-redis-credential-do-not-expose" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize("raw_score", ["1e999", "-1e999", "NaN", "Infinity", "-Infinity"])
def test_extreme_numeric_scores_return_safe_validation_errors(
    client: TestClient, raw_score: str
) -> None:
    # Raw content is intentional: normal HTTP-client JSON encoders reject
    # nonfinite values before they reach the server under test.
    response = client.post(
        "/games/chess/scores",
        content='{"user_id":"alice","score":' + raw_score + "}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    assert errors[0]["loc"] == ["body", "score"]
    assert all(set(error) == {"type", "loc", "msg"} for error in errors)
    assert client.get("/games/chess/leaderboard").json()["entries"] == []


@pytest.mark.parametrize(
    "provided_key",
    [
        None,
        "",
        "wrong-test-key-not-a-real-secret-0123456789",
        FAKE_API_KEY.upper(),
        " " + FAKE_API_KEY,
        FAKE_API_KEY + " ",
        "é".encode() * 32,
    ],
)
def test_auth_rejects_invalid_keys_before_storage_and_preserves_scores(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, provided_key: str | bytes | None
) -> None:
    submit(client, "alice", 100)
    client.headers.pop("X-API-Key")
    headers = {} if provided_key is None else {"X-API-Key": provided_key}
    unexpected_storage = AsyncMock(side_effect=AssertionError("Unauthorized storage access"))
    with monkeypatch.context() as patch:
        patch.setattr(client.app.state.redis, "execute_command", unexpected_storage)
        response = client.post(
            "/games/chess/scores", headers=headers, json={"user_id": "alice", "score": 999}
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or missing API key"}
        unexpected_storage.assert_not_called()
    assert FAKE_API_KEY not in response.text
    assert client.get("/games/chess/leaderboard").json()["entries"] == [entry("alice", 100, 1)]


@pytest.mark.parametrize("query_name", ["api_key", "X-API-Key"])
def test_auth_does_not_accept_key_in_query(client: TestClient, query_name: str) -> None:
    client.headers.pop("X-API-Key")
    response = client.post(
        "/games/chess/scores",
        params={query_name: FAKE_API_KEY},
        json={"user_id": "alice", "score": 100},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}
    assert client.get("/games/chess/leaderboard").json()["entries"] == []


def test_valid_api_key_keeps_best_score_semantics(client: TestClient) -> None:
    client.headers.pop("X-API-Key")
    for score, expected_score, updated in [(100, 100, True), (50, 100, False), (150, 150, True)]:
        response = client.post(
            "/games/chess/scores",
            headers={"X-API-Key": FAKE_API_KEY},
            json={"user_id": "alice", "score": score},
        )
        assert response.status_code == 200
        assert response.json() == {
            "game_id": "chess",
            "user_id": "alice",
            "score": expected_score,
            "rank": 1,
            "updated": updated,
        }
        assert FAKE_API_KEY not in response.text


@pytest.mark.parametrize("unneeded_header", [None, "invalid-key"])
def test_reads_health_and_documentation_are_public(
    client: TestClient, unneeded_header: str | None
) -> None:
    submit(client, "alice", 100)
    client.headers.pop("X-API-Key")
    headers = {} if unneeded_header is None else {"X-API-Key": unneeded_header}
    for path in [
        "/games/chess/leaderboard",
        "/games/chess/players/alice/context",
        "/health/live",
        "/health/ready",
        "/docs",
        "/openapi.json",
    ]:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, (path, response.text)
        assert FAKE_API_KEY not in response.text


def test_openapi_requires_key_only_for_score_submission(client: TestClient) -> None:
    client.headers.pop("X-API-Key")
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert FAKE_API_KEY not in response.text
    schema = response.json()
    scheme = schema["components"]["securitySchemes"]["ScoreSubmissionKey"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "header"
    assert scheme["name"] == "X-API-Key"
    assert not schema.get("security")
    assert schema["paths"]["/games/{game_id}/scores"]["post"]["security"] == [
        {"ScoreSubmissionKey": []}
    ]
    for methods in schema["paths"].values():
        for method, operation in methods.items():
            if method == "get":
                assert not operation.get("security")


def test_missing_submission_key_prevents_startup() -> None:
    application = create_app(Settings(_env_file=None, submission_api_key=None))
    with (
        pytest.raises(RuntimeError, match="LEADERBOARD_SUBMISSION_API_KEY"),
        TestClient(application),
    ):
        pytest.fail("The API must not start without a configured submission key.")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "short",
        "x" * 31,
        "x" * 257,
        " " * 32,
        "x" * 32 + " ",
        "x" * 16 + "\t" + "x" * 16,
        "x" * 16 + "\n" + "x" * 16,
        "é" * 32,
    ],
)
def test_invalid_submission_key_configuration_is_rejected(key: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, submission_api_key=key)


@pytest.mark.parametrize("length", [32, 256])
def test_submission_key_length_boundaries_and_secret_redaction(length: int) -> None:
    key = "k" * length
    settings = Settings(_env_file=None, submission_api_key=key)
    assert settings.submission_api_key.get_secret_value() == key
    assert key not in repr(settings)
    assert key not in str(settings)
    assert key not in settings.model_dump_json()


def test_invalid_submission_key_is_not_exposed_in_configuration_error() -> None:
    invalid_key = FAKE_API_KEY + " "
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, submission_api_key=invalid_key)
    assert FAKE_API_KEY not in str(error.value)
