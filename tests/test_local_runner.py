"""The local runner must never adopt a foreign process or silently switch stores."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "local.py"
    spec = importlib.util.spec_from_file_location("local_runner_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = tmp_path / "local"
    runtime.mkdir()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "RUNTIME", runtime)
    monkeypatch.setattr(module, "DATA", tmp_path / "redis")
    settings = SimpleNamespace(redis_url="redis://127.0.0.1:6379/0", submission_api_key=object())
    monkeypatch.setattr(module, "Settings", lambda **kwargs: settings)
    monkeypatch.setattr(module, "port_in_use", lambda port: False)
    return module


def test_process_identity_reads_start_time_with_parentheses_in_name(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    stat = "1234 (python worker ) helper) " + " ".join(["S", *(["0"] * 18), "123456"])
    monkeypatch.setattr(Path, "read_text", lambda self: stat)
    assert runner.process_identity(1234) == "123456"


def test_process_identity_treats_zombie_as_stopped(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    stat = "1234 (python) " + " ".join(["Z", *(["0"] * 18), "123456"])
    monkeypatch.setattr(Path, "read_text", lambda self: stat)
    assert runner.process_identity(1234) is None


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_process_identity_handles_disappeared_process(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def disappeared(self: Path) -> str:
        raise error

    monkeypatch.setattr(Path, "read_text", disappeared)
    assert runner.process_identity(1234) is None


@pytest.mark.parametrize(
    "actual_start,expected", [("123456", 1234), ("987654", None), (None, None)]
)
def test_owned_pid_requires_original_process_start_time(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    actual_start: str | None,
    expected: int | None,
) -> None:
    (runner.RUNTIME / "api.json").write_text(json.dumps({"pid": 1234, "started": "123456"}))
    monkeypatch.setattr(runner, "process_identity", lambda pid: actual_start)
    assert runner.owned_pid("api") == expected


def test_owned_pid_does_not_adopt_process_without_metadata(runner: ModuleType) -> None:
    assert runner.owned_pid("api") is None


@pytest.mark.parametrize(
    "url,expected_port",
    [
        ("redis://localhost", 6379),
        ("redis://127.0.0.1:6379/0", 6379),
        ("redis://localhost:6380/0", 6380),
    ],
)
def test_validate_accepts_free_local_database_zero(
    runner: ModuleType, url: str, expected_port: int
) -> None:
    runner.Settings().redis_url = url
    assert runner.validate_runtime() == expected_port


@pytest.mark.parametrize(
    "url",
    [
        "redis://example.invalid:6379/0",
        "rediss://127.0.0.1:6379/0",
        "redis://user@localhost:6379/0",
        "redis://:not-a-real-secret@localhost:6379/0",
        "redis://localhost:6379/1",
        "redis://localhost:6379/0?socket_timeout=1",
    ],
)
def test_validate_rejects_unsupported_redis_configuration(runner: ModuleType, url: str) -> None:
    runner.Settings().redis_url = url
    with pytest.raises(RuntimeError):
        runner.validate_runtime()


@pytest.mark.parametrize("recorded_port", [6379, None])
def test_validate_rejects_changed_or_unknown_managed_redis_port(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, recorded_port: int | None
) -> None:
    runner.Settings().redis_url = "redis://localhost:6380/0"
    state = {"pid": 1234, "started": "123456", "port": recorded_port}
    (runner.RUNTIME / "redis.json").write_text(json.dumps(state))
    monkeypatch.setattr(runner, "process_identity", lambda pid: "123456")
    with pytest.raises(RuntimeError, match="differs from the managed server"):
        runner.validate_runtime()


def test_validate_accepts_matching_managed_redis_port(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"pid": 1234, "started": "123456", "port": 6379}
    (runner.RUNTIME / "redis.json").write_text(json.dumps(state))
    monkeypatch.setattr(runner, "process_identity", lambda pid: "123456")
    monkeypatch.setattr(runner, "port_in_use", lambda port: port == 6379)
    assert runner.validate_runtime() == 6379


@pytest.mark.parametrize("foreign_port", [8000, 6379])
def test_validate_refuses_foreign_listener(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, foreign_port: int
) -> None:
    monkeypatch.setattr(runner, "port_in_use", lambda port: port == foreign_port)
    with pytest.raises(RuntimeError, match="owned by another process"):
        runner.validate_runtime()


def test_validate_requires_submission_key(runner: ModuleType) -> None:
    runner.Settings().submission_api_key = None
    with pytest.raises(RuntimeError, match="SUBMISSION_API_KEY"):
        runner.validate_runtime()


def test_restart_validates_before_stopping_anything(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["local.py", "restart"])
    runner.Settings().redis_url = "redis://localhost:6379/1"
    actions = []
    monkeypatch.setattr(runner, "stop_api", lambda: actions.append("stop"))
    monkeypatch.setattr(runner, "start", lambda: actions.append("start"))
    with pytest.raises(RuntimeError, match="database 0"):
        runner.main()
    assert actions == []
