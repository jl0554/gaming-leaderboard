"""Manage the interview-container demo; never stop an unowned process or erase data."""

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from leaderboard.config import Settings

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "local"
DATA = ROOT / ".runtime" / "redis"


def process_identity(pid: int) -> str | None:
    """Linux start time prevents a stale PID file from targeting a reused PID."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def owned_pid(name: str) -> int | None:
    state_file = RUNTIME / f"{name}.json"
    if not state_file.exists():
        return None
    state = json.loads(state_file.read_text())
    pid = state["pid"]
    if process_identity(pid) == state["started"]:
        return pid
    return None


def port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def launch(name: str, command: list[str], *, port: int) -> None:
    with (RUNTIME / f"{name}.log").open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    identity = process_identity(process.pid)
    if identity is None:
        raise RuntimeError(f"{name} failed to start; see {RUNTIME / (name + '.log')}")
    (RUNTIME / f"{name}.json").write_text(
        json.dumps({"pid": process.pid, "started": identity, "port": port})
    )


def stop_api() -> None:
    pid = owned_pid("api")
    if pid is None:
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if owned_pid("api") is None and not port_in_use(8000):
            return
        time.sleep(0.1)
    raise RuntimeError("API did not stop cleanly; inspect its log. No force-kill was attempted.")


def ready() -> bool:
    try:
        with urlopen("http://127.0.0.1:8000/health/ready", timeout=2) as response:
            return response.status == 200 and json.load(response).get("status") == "ok"
    except (OSError, ValueError):
        return False


def validate_runtime() -> int:
    settings = Settings(_env_file=ROOT / ".env")
    endpoint = urlsplit(settings.redis_url)
    if endpoint.scheme != "redis" or endpoint.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("This local helper requires a local redis:// endpoint.")
    if endpoint.username or endpoint.password or endpoint.path not in {"", "/0"} or endpoint.query:
        raise RuntimeError(
            "This demo helper requires Redis database 0 without authentication/options."
        )
    port = endpoint.port or 6379
    if settings.submission_api_key is None:
        raise RuntimeError("Configure LEADERBOARD_SUBMISSION_API_KEY in .env first.")
    if owned_pid("api") is None and port_in_use(8000):
        raise RuntimeError("Port 8000 is owned by another process; it was not stopped.")
    if owned_pid("redis") is not None:
        state = json.loads((RUNTIME / "redis.json").read_text())
        if state.get("port") != port:
            raise RuntimeError(
                "Configured Redis port differs from the managed server. Restore the previous URL; "
                "changing Redis endpoints requires an explicit data migration. Nothing was stopped."
            )
    elif port_in_use(port):
        raise RuntimeError("Redis port is owned by another process; it was not reused or stopped.")
    return port


def start() -> None:
    port = validate_runtime()
    if owned_pid("redis") is None:
        DATA.mkdir(parents=True, exist_ok=True)
        launch(
            "redis",
            [
                "redis-server",
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--dir",
                str(DATA),
                "--appendonly",
                "yes",
                "--appendfsync",
                "everysec",
            ],
            port=port,
        )
    if owned_pid("api") is None:
        launch(
            "api",
            [
                str(ROOT / ".venv" / "bin" / "python"),
                "-m",
                "uvicorn",
                "leaderboard.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--reload",
                "--reload-dir",
                str(ROOT / "src"),
                "--no-access-log",
            ],
            port=8000,
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ready():
            print("READY: http://localhost:8000/docs (forward container port 8000 in your IDE)")
            print(f"Redis data: {DATA}; logs: {RUNTIME}")
            return
        time.sleep(0.2)
    raise RuntimeError(f"API did not become ready; see logs in {RUNTIME}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "restart", "stop", "status"])
    command = parser.parse_args().command
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if command == "status":
            print(f"Managed API running: {owned_pid('api') is not None}")
            print(f"Managed Redis running: {owned_pid('redis') is not None}")
            print(f"API ready: {ready()}")
        elif command == "stop":
            stop_api()
            print("API stopped; Redis and its data are preserved.")
        else:
            if command == "restart":
                validate_runtime()
                stop_api()
            start()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
