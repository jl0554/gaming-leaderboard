"""Exercise an isolated running stack; run inside its API container, not production."""

import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


def main() -> None:
    if os.environ.get("LEADERBOARD_ENVIRONMENT") != "test":
        raise SystemExit(
            "Smoke test requires LEADERBOARD_ENVIRONMENT=test; it creates test scores."
        )
    submission_key = os.environ["LEADERBOARD_SUBMISSION_API_KEY"]
    metrics_key = os.environ["LEADERBOARD_METRICS_API_KEY"]
    game = "smoke-" + uuid4().hex
    base_url = "http://127.0.0.1:8000"

    def request(path: str, *, payload: dict | None = None, key: str | None = None) -> tuple:
        headers = {}
        if key is not None:
            headers["X-API-Key"] = key
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = Request(base_url + path, data=data, headers=headers)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            assert response.headers.get("X-Request-ID"), "Missing request trace ID"
            return response.status, json.load(response)

    for path in ["/health/live", "/health/ready"]:
        status, body = request(path)
        assert status == 200 and body["status"] == "ok", "Health check failed"
    status, _ = request(f"/games/{game}/scores", payload={"user_id": "alice", "score": 100})
    assert status == 401, "Unauthenticated write was not rejected"
    for user, score, updated in [
        ("alice", 100, True),
        ("bob", 100, True),
        ("carol", 90, True),
        ("alice", 50, False),
    ]:
        status, body = request(
            f"/games/{game}/scores", payload={"user_id": user, "score": score}, key=submission_key
        )
        assert status == 200 and body["updated"] is updated, "Score submission failed"
    status, body = request(f"/games/{game}/leaderboard")
    assert status == 200
    assert body["entries"] == [
        {"user_id": "alice", "score": 100, "rank": 1},
        {"user_id": "bob", "score": 100, "rank": 1},
        {"user_id": "carol", "score": 90, "rank": 3},
    ], "Best scores or shared ranks are incorrect"
    status, body = request(f"/games/{game}/players/bob/context")
    assert status == 200 and body["player"]["rank"] == 1
    assert body["above"][0]["user_id"] == "alice"
    assert body["below"][0]["rank"] == 3
    status, _ = request("/metrics")
    assert status == 401, "Metrics must require its own credential"
    with urlopen(
        Request(base_url + "/metrics", headers={"X-Metrics-Key": metrics_key}), timeout=5
    ) as response:
        assert response.status == 200
        assert b"leaderboard_http_requests_total" in response.read()
    print("Docker smoke passed: readiness, auth, best scores, ties, context, tracing, metrics.")


if __name__ == "__main__":
    main()
