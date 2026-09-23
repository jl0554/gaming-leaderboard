from fastapi.testclient import TestClient

from leaderboard.config import Settings
from leaderboard.main import create_app


def test_liveness() -> None:
    application = create_app(
        Settings(
            _env_file=None,
            submission_api_key="test-only-system-key-not-a-real-secret-0123456789",
        )
    )
    with TestClient(application) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Gaming Leaderboard",
    }
