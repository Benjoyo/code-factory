from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from code_factory.errors import ReviewError
from code_factory.workspace.review_models import ReviewTarget
from code_factory.workspace.review_ports import (
    _listening_pid,
    _process_command,
    ensure_review_port_available,
    review_host,
    review_port_in_use,
    review_port_owner_details,
)


def _launch(*, port: int | None, url: str | None) -> Any:
    return SimpleNamespace(name="app", port=port, url=url)


def test_ensure_review_port_available_covers_free_and_conflict_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ReviewTarget("main", "main", None, None, "origin/main")
    ensure_review_port_available(target, _launch(port=None, url=None))

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.review_port_in_use",
        lambda host, port: False,
    )
    ensure_review_port_available(
        target, _launch(port=8000, url="http://127.0.0.1:8000")
    )

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.review_port_in_use",
        lambda host, port: True,
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_ports.review_port_owner_details",
        lambda port: None,
    )
    with pytest.raises(ReviewError, match="port 8000 .* already in use"):
        ensure_review_port_available(
            target, _launch(port=8000, url="http://127.0.0.1:8000")
        )

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.review_port_owner_details",
        lambda port: "Occupying PID: 123 (`python`).",
    )
    with pytest.raises(ReviewError, match="Occupying PID: 123"):
        ensure_review_port_available(
            target, _launch(port=8000, url="http://127.0.0.1:8000")
        )


def test_review_host_and_port_probe_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert review_host(None) == "127.0.0.1"
    assert review_host("http://localhost:3000") == "localhost"

    class FakeSocket:
        def __init__(self, status: int) -> None:
            self._status = status

        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def connect_ex(self, address: tuple[str, int]) -> int:
            assert address == ("127.0.0.1", 8000)
            return self._status

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.socket.socket",
        lambda *_args, **_kwargs: FakeSocket(0),
    )
    assert review_port_in_use("127.0.0.1", 8000) is True

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.socket.socket",
        lambda *_args, **_kwargs: FakeSocket(1),
    )
    assert review_port_in_use("127.0.0.1", 8000) is False


def test_review_port_owner_details_and_process_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "code_factory.workspace.review_ports._listening_pid", lambda port: None
    )
    assert review_port_owner_details(8000) is None

    monkeypatch.setattr(
        "code_factory.workspace.review_ports._listening_pid", lambda port: 123
    )
    monkeypatch.setattr(
        "code_factory.workspace.review_ports._process_command", lambda pid: None
    )
    assert review_port_owner_details(8000) == "Occupying PID: 123."

    monkeypatch.setattr(
        "code_factory.workspace.review_ports._process_command",
        lambda pid: "python -m uvicorn",
    )
    assert "python -m uvicorn" in cast(str, review_port_owner_details(8000))

    def raise_oserror(*args: Any, **kwargs: Any) -> Any:
        raise OSError("missing")

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.subprocess.run", raise_oserror
    )
    assert _listening_pid(8000) is None
    assert _process_command(123) is None

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert _listening_pid(8000) is None
    assert _process_command(123) is None

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="x\n"),
    )
    assert _listening_pid(8000) is None

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="p456\n"),
    )
    assert _listening_pid(8000) == 456

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="   \n"),
    )
    assert _process_command(123) is None

    monkeypatch.setattr(
        "code_factory.workspace.review_ports.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="python app.py\n"),
    )
    assert _process_command(123) == "python app.py"
