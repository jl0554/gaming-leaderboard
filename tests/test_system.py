from fastapi.testclient import TestClient

from leaderboard.main import app


def test_liveness() -> None:
    with TestClient(app) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Gaming Leaderboard",
    }
