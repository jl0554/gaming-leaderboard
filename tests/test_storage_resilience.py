"""Real process failures and orderly AOF recovery, not a crash durability guarantee."""

import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Timer
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from leaderboard.config import Settings
from leaderboard.main import create_app


class OwnedRedis:
    """A process and files owned by one test; never connect to shared Redis."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.log_path = directory / "redis.log"
        self.process: subprocess.Popen | None = None
        self.executable = shutil.which("redis-server")
        if self.executable is None:
            pytest.fail("Resilience tests require redis-server on PATH.")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"redis://127.0.0.1:{self.port}/0"

    def connection(self) -> Redis:
        return Redis.from_url(
            self.url,
            decode_responses=True,
            socket_timeout=0.2,
            socket_connect_timeout=0.2,
            retry=Retry(NoBackoff(), 0),
        )

    def start(self) -> None:
        assert self.process is None or self.process.poll() is not None
        with self.log_path.open("ab") as output:
            self.process = subprocess.Popen(
                [
                    self.executable,
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--dir",
                    str(self.directory),
                    "--save",
                    "",
                    "--appendonly",
                    "yes",
                    "--appendfsync",
                    "everysec",
                    "--aof-use-rdb-preamble",
                    "no",
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        with self.connection() as probe:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    pytest.fail(f"Redis failed to start: {self.log_path.read_text()}")
                try:
                    if probe.ping():
                        return
                except RedisError:
                    time.sleep(0.02)
        pytest.fail(f"Redis did not become ready: {self.log_path.read_text()}")

    def resume(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGCONT)

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.resume()  # Cleanup must work even after a failed SIGSTOP test.
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            pytest.fail("Owned Redis failed to shut down cleanly within five seconds.")
        assert self.process.returncode == 0, self.log_path.read_text()


@pytest.fixture
def owned_redis(tmp_path: Path) -> Iterator[OwnedRedis]:
    server = OwnedRedis(tmp_path)
    try:
        server.start()
        yield server
    finally:
        server.stop()


@pytest.fixture
def resilience_client(owned_redis: OwnedRedis) -> Iterator[TestClient]:
    application = create_app(
        Settings(
            _env_file=None,
            redis_url=owned_redis.url,
            redis_key_prefix=f"resilience-{uuid4().hex}",
            redis_socket_timeout=0.15,
            redis_connect_timeout=0.15,
        )
    )
    with TestClient(application) as client:
        yield client


def submit(client: TestClient, user_id: str, score: int, game: str = "chess") -> dict:
    response = client.post(f"/games/{game}/scores", json={"user_id": user_id, "score": score})
    assert response.status_code == 200, response.text
    return response.json()


def assert_unavailable(response) -> None:
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "REDIS_UNAVAILABLE"
    assert response.json()["error"]["message"]
    assert "Traceback" not in response.text


def test_actual_redis_outage_returns_503_and_same_client_recovers(
    owned_redis: OwnedRedis, resilience_client: TestClient
) -> None:
    submit(resilience_client, "alice", 100)
    owned_redis.stop()
    started = time.monotonic()
    assert_unavailable(resilience_client.get("/games/chess/leaderboard"))
    assert_unavailable(resilience_client.get("/games/chess/players/alice/context"))
    assert_unavailable(
        resilience_client.post("/games/chess/scores", json={"user_id": "alice", "score": 200})
    )
    assert time.monotonic() - started < 3, "Unavailable storage must fail promptly."

    owned_redis.start()
    response = resilience_client.get("/games/chess/leaderboard")
    assert response.status_code == 200
    assert response.json()["entries"] == [{"user_id": "alice", "score": 100, "rank": 1}]
    assert submit(resilience_client, "alice", 120)["score"] == 120


def test_real_socket_timeout_recovers_after_owned_redis_resumes(
    owned_redis: OwnedRedis, resilience_client: TestClient
) -> None:
    submit(resilience_client, "alice", 100)
    assert owned_redis.process is not None
    # If application timeout handling regresses, resume Redis to release the call.
    # This outer guard is deliberately much longer than the configured 150 ms timeout.
    guard = Timer(5, owned_redis.resume)
    guard.daemon = True
    guard.start()
    try:
        owned_redis.process.send_signal(signal.SIGSTOP)
        _, status = os.waitpid(owned_redis.process.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status)
        started = time.monotonic()
        response = resilience_client.get("/games/chess/leaderboard")
        elapsed = time.monotonic() - started
        assert_unavailable(response)
        assert elapsed < 2, f"Configured socket timeout was not respected: {elapsed:.3f}s"
    finally:
        owned_redis.resume()
        guard.cancel()
        guard.join(timeout=1)

    response = resilience_client.get("/games/chess/players/alice/context")
    assert response.status_code == 200
    assert response.json()["player"] == {"user_id": "alice", "score": 100, "rank": 1}
    assert submit(resilience_client, "alice", 120)["score"] == 120


def test_aof_orderly_restart_preserves_scores_shared_ranks_and_retry_semantics(
    owned_redis: OwnedRedis, resilience_client: TestClient
) -> None:
    # Validate effective Redis settings; disabling both RDB paths proves AOF recovery.
    with owned_redis.connection() as admin:
        assert admin.config_get("appendonly") == {"appendonly": "yes"}
        assert admin.config_get("appendfsync") == {"appendfsync": "everysec"}
        assert admin.config_get("save") == {"save": ""}
        assert admin.config_get("aof-use-rdb-preamble") == {"aof-use-rdb-preamble": "no"}
        assert admin.info("persistence")["aof_enabled"] == 1

    for user_id, score in [("bob", 100), ("alice", 100), ("carol", 90), ("zero", 0)]:
        submit(resilience_client, user_id, score)
    submit(resilience_client, "alice", 700, game="racing")
    expected = [
        {"user_id": "alice", "score": 100, "rank": 1},
        {"user_id": "bob", "score": 100, "rank": 1},
        {"user_id": "carol", "score": 90, "rank": 3},
        {"user_id": "zero", "score": 0, "rank": 4},
    ]
    assert resilience_client.get("/games/chess/leaderboard").json()["entries"] == expected

    # SIGTERM requests a clean Redis shutdown. This does NOT test power loss or SIGKILL,
    # and everysec persistence can still lose recent acknowledged writes in a crash.
    owned_redis.stop()
    assert not list(owned_redis.directory.rglob("*.rdb"))
    assert any(path.stat().st_size > 0 for path in owned_redis.directory.rglob("*.aof"))
    owned_redis.start()  # Reuse the same port and persistence directory.

    response = resilience_client.get("/games/chess/leaderboard")
    assert response.status_code == 200
    assert response.json()["entries"] == expected
    context = resilience_client.get("/games/chess/players/bob/context")
    assert context.status_code == 200
    assert context.json() == {
        "game_id": "chess",
        "player": expected[1],
        "above": [expected[0]],
        "below": [expected[2]],
    }
    assert resilience_client.get("/games/racing/leaderboard").json()["entries"] == [
        {"user_id": "alice", "score": 700, "rank": 1}
    ]
    for score in [100, 50]:
        assert submit(resilience_client, "bob", score) == {
            "game_id": "chess",
            "user_id": "bob",
            "score": 100,
            "rank": 1,
            "updated": False,
        }
    assert submit(resilience_client, "bob", 150) == {
        "game_id": "chess",
        "user_id": "bob",
        "score": 150,
        "rank": 1,
        "updated": True,
    }
    assert resilience_client.get("/games/chess/leaderboard").json()["entries"] == [
        {"user_id": "bob", "score": 150, "rank": 1},
        {"user_id": "alice", "score": 100, "rank": 2},
        {"user_id": "carol", "score": 90, "rank": 3},
        {"user_id": "zero", "score": 0, "rank": 4},
    ]
