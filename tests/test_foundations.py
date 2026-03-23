from __future__ import annotations

import asyncio
import logging
import runpy
import shlex
import signal
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from logging import NullHandler
from pathlib import Path
from typing import Any, cast

import click
import pytest
from typer.testing import CliRunner

from code_factory.application import CodeFactoryService
from code_factory.application.dashboard_diagnostics import (
    DashboardDiagnostics,
    DashboardDiagnosticsHandler,
)
from code_factory.application.logging import configure_logging
from code_factory.cli import (
    ACK_FLAG,
    CLIConfig,
    acknowledgement_banner,
    app,
    build_cli_config,
    main,
    normalize_cli_args,
)
from code_factory.config import (
    max_concurrent_agents_for_state,
    validate_dispatch_settings,
)
from code_factory.config.utils import (
    boolean,
    coerce_int,
    configured_active_states,
    env_reference_name,
    non_negative_int,
    normalize_keys,
    normalize_path_token,
    normalize_secret_value,
    normalize_state_limits,
    optional_non_negative_int,
    optional_string,
    positive_int,
    require_mapping,
    required_command,
    resolve_env_value,
    resolve_path_value,
    resolve_secret_setting,
    string_list,
    string_with_default,
)
from code_factory.errors import ConfigValidationError, WorkflowLoadError, WorkspaceError
from code_factory.issues import Issue, normalize_issue_state
from code_factory.prompts.values import to_liquid_value
from code_factory.runtime.support import maybe_aclose
from code_factory.structured_results import (
    StructuredTurnResult,
    normalize_structured_turn_result,
    parse_result_comment,
    render_result_comment,
    structured_turn_output_schema,
)
from code_factory.workflow.loader import (
    DEFAULT_WORKFLOW_FILENAME,
    current_stamp,
    finalize_prompt_section,
    front_matter_yaml_to_map,
    parse_prompt_sections,
    split_front_matter,
    workflow_file_path,
)
from code_factory.workflow.state_profiles import parse_state_profiles
from code_factory.workflow.store import WorkflowStoreActor
from code_factory.workflow.template import (
    WorkflowTemplateValues,
    render_default_workflow,
)
from code_factory.workspace.hooks import run_hook
from code_factory.workspace.manager import WorkspaceManager
from code_factory.workspace.paths import (
    canonicalize,
    is_within,
    safe_identifier,
    validate_workspace_path,
    workspace_path_for_issue,
)
from code_factory.workspace.utils import (
    clean_tmp_artifacts,
    ensure_workspace,
    issue_context,
)

from .conftest import make_issue, make_snapshot, write_workflow_file

runner = CliRunner()


def test_normalize_cli_args_routes_bare_service_invocations() -> None:
    assert normalize_cli_args([]) == ["serve"]
    assert normalize_cli_args([ACK_FLAG]) == ["serve", ACK_FLAG]
    assert normalize_cli_args(["workflow.md"]) == ["serve", "workflow.md"]
    assert normalize_cli_args(["serve", ACK_FLAG]) == ["serve", ACK_FLAG]
    assert normalize_cli_args(["init"]) == ["init"]
    assert normalize_cli_args(["--help"]) == ["--help"]


def test_build_cli_config_resolves_logs_root_port_and_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = build_cli_config(
        Path("workflow.md"),
        Path("~/logs"),
        9000,
    )

    assert result == CLIConfig(
        workflow_path=str((tmp_path / "workflow.md").resolve()),
        logs_root=str(Path("~/logs").expanduser().resolve()),
        port=9000,
    )
    defaulted = build_cli_config(None, None, None)
    assert defaulted.workflow_path == str(
        (tmp_path / DEFAULT_WORKFLOW_FILENAME).resolve()
    )


def test_cli_help_lists_init_and_serve() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "init" in result.output
    assert "serve" in result.output
    assert "create a starter workflow" in result.output.lower()


def test_acknowledgement_banner_has_consistent_frame() -> None:
    banner = acknowledgement_banner()
    lines = banner.splitlines()
    assert lines[0].startswith("╭")
    assert lines[-1].startswith("╰")
    assert len({len(line) for line in lines}) == 1


def test_serve_command_requires_acknowledgement() -> None:
    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 1
    assert ACK_FLAG in result.output
    assert "low key engineering preview" in result.output


def test_init_command_copies_default_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    values = WorkflowTemplateValues(
        tracker_kind="linear",
        project_slug="demo-project",
        git_repo="git@github.com:example/demo.git",
        active_states=("Todo", "In Progress"),
        terminal_states=("Done",),
        workspace_root="/tmp/demo-workspaces",
        max_concurrent_agents=3,
    )
    monkeypatch.setattr("code_factory.cli.prompt_project_init", lambda **_: values)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert "Created" in result.output
    assert (tmp_path / DEFAULT_WORKFLOW_FILENAME).read_text(
        encoding="utf-8"
    ) == render_default_workflow(values)
    assert (tmp_path / ".agents" / "skills" / "commit" / "SKILL.md").is_file()


def test_init_command_rejects_existing_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "code_factory.cli.prompt_project_init",
        lambda **_: WorkflowTemplateValues(
            tracker_kind="linear",
            project_slug="demo-project",
            git_repo="git@github.com:example/demo.git",
            active_states=("Todo",),
            terminal_states=("Done",),
            workspace_root="/tmp/demo-workspaces",
            max_concurrent_agents=2,
        ),
    )
    workflow = tmp_path / DEFAULT_WORKFLOW_FILENAME
    workflow.write_text("existing\n", encoding="utf-8")

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert workflow.read_text(encoding="utf-8") == "existing\n"


def test_init_command_force_overwrites_existing_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    values = WorkflowTemplateValues(
        tracker_kind="memory",
        project_slug="demo-project",
        git_repo="https://github.com/example/demo.git",
        active_states=("Queued",),
        terminal_states=("Done",),
        workspace_root="/tmp/demo-workspaces",
        max_concurrent_agents=4,
    )
    monkeypatch.setattr("code_factory.cli.prompt_project_init", lambda **_: values)
    workflow = tmp_path / DEFAULT_WORKFLOW_FILENAME
    workflow.write_text("existing\n", encoding="utf-8")
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "old.txt").write_text("old\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--force"])

    assert result.exit_code == 0
    assert workflow.read_text(encoding="utf-8") == render_default_workflow(values)
    assert not (skills_dir / "old.txt").exists()
    assert (skills_dir / "land" / "land_watch.py").is_file()


def test_main_returns_acknowledgement_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 1
    assert ACK_FLAG in capsys.readouterr().err


def test_main_returns_click_exception_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeCommand:
        def main(self, **_: Any) -> None:
            raise click.ClickException("bad args")

    monkeypatch.setattr("typer.main.get_command", lambda _: FakeCommand())

    assert main(["serve"]) == 1
    assert "bad args" in capsys.readouterr().err


def test_main_returns_click_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCommand:
        def main(self, **_: Any) -> None:
            raise click.exceptions.Exit(7)

    monkeypatch.setattr("typer.main.get_command", lambda _: FakeCommand())

    assert main(["serve"]) == 7


def test_main_rejects_missing_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([ACK_FLAG, str(tmp_path / "missing.md")]) == 1
    assert "Workflow file not found" in capsys.readouterr().err


def test_main_runs_service_and_handles_keyboard_interrupt(tmp_path: Path) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("prompt\n", encoding="utf-8")
    calls: list[tuple[str, str | None, int | None]] = []

    class FakeService:
        def __init__(
            self,
            workflow_path: str,
            *,
            logs_root: str | None = None,
            port_override: int | None = None,
        ) -> None:
            calls.append((workflow_path, logs_root, port_override))

        async def run_forever(self) -> None:
            return None

    try:
        import code_factory.cli as cli_module

        cli_module.CodeFactoryService = FakeService  # type: ignore[assignment]
        assert main([ACK_FLAG, "--port", "4567", str(workflow)]) == 0
        assert calls == [(str(workflow.resolve()), None, 4567)]

        class InterruptingService(FakeService):
            async def run_forever(self) -> None:
                raise KeyboardInterrupt

        cli_module.CodeFactoryService = InterruptingService  # type: ignore[assignment]
        assert main([ACK_FLAG, str(workflow)]) == 130
    finally:
        import code_factory.cli as cli_module

        cli_module.CodeFactoryService = CodeFactoryService  # type: ignore[assignment]


def test___main___raises_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("code_factory.cli.main", lambda: 7)
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("code_factory.__main__", run_name="__main__")
    assert excinfo.value.code == 7


def test_configure_logging_adds_stream_and_file_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_handlers: list[Any] = []

    class FakeFileHandler:
        def __init__(self, path: Path, maxBytes: int, backupCount: int) -> None:
            self.path = Path(path)
            self.maxBytes = maxBytes
            self.backupCount = backupCount
            self.formatter = None
            created_handlers.append(self)

        def setFormatter(self, formatter: Any) -> None:
            self.formatter = formatter

    monkeypatch.setattr(
        "code_factory.application.logging.RotatingFileHandler", FakeFileHandler
    )

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        log_path = configure_logging(str(tmp_path))

        assert log_path == tmp_path / "log" / "code-factory.log"
        assert root_logger.level == logging.INFO
        assert len(root_logger.handlers) == 2
        assert created_handlers[0].path == log_path
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert logging.getLogger("aiohttp.access").level == logging.WARNING
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)


def test_configure_logging_reuses_existing_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        root_logger.addHandler(logging.NullHandler())
        log_path = configure_logging(str(tmp_path))
        assert log_path == tmp_path / "log" / "code-factory.log"
        assert configure_logging(None) is None
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)


def test_configure_logging_without_logs_root_uses_stream_only() -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        assert configure_logging(None) is None
        assert len(root_logger.handlers) == 1
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)


def test_configure_logging_without_console_and_with_logs_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_handlers: list[Any] = []

    class FakeFileHandler:
        def __init__(self, path: Path, maxBytes: int, backupCount: int) -> None:
            self.path = Path(path)
            created_handlers.append(self)

        def setFormatter(self, formatter: Any) -> None:
            return None

    monkeypatch.setattr(
        "code_factory.application.logging.RotatingFileHandler", FakeFileHandler
    )
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        log_path = configure_logging(str(tmp_path), console=False)
        assert log_path == tmp_path / "log" / "code-factory.log"
        assert len(root_logger.handlers) == 1
        assert created_handlers[0].path == log_path
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)


def test_configure_logging_supports_dashboard_mode_without_console() -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        assert configure_logging(None, console=False) is None
        assert len(root_logger.handlers) == 1
        assert isinstance(root_logger.handlers[0], NullHandler)
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)


def test_configure_logging_dashboard_diagnostics_capture_multiline_errors() -> None:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    diagnostics = DashboardDiagnostics()
    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        assert configure_logging(None, console=False, diagnostics=diagnostics) is None
        logger = logging.getLogger("code_factory.tests")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("hook failed\noutput:\nline 1\nline 2")
        entries = diagnostics.entries()
        assert len(entries) == 1
        assert entries[0].level == "ERROR"
        assert "hook failed" in entries[0].message
        assert "line 1" in entries[0].message
        assert "RuntimeError: boom" in entries[0].message
        assert configure_logging(None, console=False, diagnostics=diagnostics) is None
        assert (
            len(
                [
                    handler
                    for handler in root_logger.handlers
                    if isinstance(handler, DashboardDiagnosticsHandler)
                ]
            )
            == 1
        )
        other_diagnostics = DashboardDiagnostics()
        assert (
            configure_logging(None, console=False, diagnostics=other_diagnostics)
            is None
        )
        assert (
            len(
                [
                    handler
                    for handler in root_logger.handlers
                    if isinstance(handler, DashboardDiagnosticsHandler)
                ]
            )
            == 2
        )
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)


def test_service_dashboard_helpers_and_logging_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", server={"port": 4321})
    snapshot = make_snapshot(workflow)
    service = CodeFactoryService(str(workflow))

    monkeypatch.setattr(
        "code_factory.application.service.LiveStatusDashboard.enabled",
        lambda settings, stream: True,
    )
    dashboard = service._build_status_dashboard(snapshot, cast(Any, object()))
    assert dashboard is not None

    monkeypatch.setattr(
        "code_factory.application.service.LiveStatusDashboard.enabled",
        lambda settings, stream: False,
    )
    assert service._build_status_dashboard(snapshot, cast(Any, object())) is None
    assert service._effective_port(snapshot) == 4321

    calls: list[tuple[str | None, bool | None]] = []

    def fake_configure_logging(
        logs_root: str | None, *, console: bool = True
    ) -> Path | None:
        calls.append((logs_root, console))
        return None

    monkeypatch.setattr(
        "code_factory.application.service.configure_logging", fake_configure_logging
    )
    assert service._configure_logging(True) is None
    assert calls == [(None, False)]

    monkeypatch.setattr(
        "code_factory.application.service.configure_logging",
        lambda logs_root, *, console=True: Path("/tmp/diagnostics.log"),
    )
    assert service._configure_logging(
        False, diagnostics=DashboardDiagnostics()
    ) == Path("/tmp/diagnostics.log")

    def old_configure_logging(logs_root: str | None) -> Path | None:
        return Path("/tmp/fallback.log")

    monkeypatch.setattr(
        "code_factory.application.service.configure_logging", old_configure_logging
    )
    assert service._configure_logging(False) == Path("/tmp/fallback.log")

    compat_calls = 0

    def staged_old_configure_logging(
        logs_root: str | None,
        *,
        console: bool = True,
        diagnostics: DashboardDiagnostics | None = None,
    ) -> Path | None:
        nonlocal compat_calls
        compat_calls += 1
        if compat_calls == 1:
            raise TypeError("unexpected keyword argument 'diagnostics'")
        if compat_calls == 2:
            raise TypeError("unexpected keyword argument 'console'")
        return Path("/tmp/staged-fallback.log")

    monkeypatch.setattr(
        "code_factory.application.service.configure_logging",
        staged_old_configure_logging,
    )
    assert service._configure_logging(
        False, diagnostics=DashboardDiagnostics()
    ) == Path("/tmp/staged-fallback.log")

    monkeypatch.setattr(
        "code_factory.application.service.configure_logging", old_configure_logging
    )
    assert service._configure_logging(
        False, diagnostics=DashboardDiagnostics()
    ) == Path("/tmp/fallback.log")

    def bad_typeerror(logs_root: str | None, *, console: bool = True) -> Path | None:
        raise TypeError("different")

    monkeypatch.setattr(
        "code_factory.application.service.configure_logging", bad_typeerror
    )
    with pytest.raises(TypeError, match="different"):
        service._configure_logging(False)

    def bad_typeerror_with_diagnostics(
        logs_root: str | None,
        *,
        console: bool = True,
        diagnostics: DashboardDiagnostics | None = None,
    ) -> Path | None:
        raise TypeError("different")

    monkeypatch.setattr(
        "code_factory.application.service.configure_logging",
        bad_typeerror_with_diagnostics,
    )
    with pytest.raises(TypeError, match="different"):
        service._configure_logging(False, diagnostics=DashboardDiagnostics())

    monkeypatch.setattr("code_factory.application.service.sys.stdin", object())
    assert service._dashboard_input_supported() is False


@pytest.mark.asyncio
async def test_service_monitor_dashboard_input_and_signal_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    service = CodeFactoryService(str(workflow))
    stop_event = asyncio.Event()

    class NoFileno:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("code_factory.application.service.sys.stdin", NoFileno())
    await service._monitor_dashboard_input(stop_event)
    assert stop_event.is_set() is False

    callbacks: list[Any] = []
    removed: list[int] = []

    class FakeLoop:
        def add_reader(self, fd: int, callback: Any) -> None:
            callbacks.append(callback)

        def remove_reader(self, fd: int) -> None:
            removed.append(fd)

    class FakeStdin:
        def __init__(self, text: str) -> None:
            self._text = text

        def fileno(self) -> int:
            return 4

        def readline(self) -> str:
            return self._text

    monkeypatch.setattr("asyncio.get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr("code_factory.application.service.sys.stdin", FakeStdin("q\n"))
    task = asyncio.create_task(service._monitor_dashboard_input(stop_event))
    await asyncio.sleep(0)
    callbacks[0]()
    await task
    assert stop_event.is_set() is True
    assert removed == [4]

    class NoReaderLoop:
        def add_reader(self, fd: int, callback: Any) -> None:
            raise NotImplementedError

    stop_event = asyncio.Event()
    monkeypatch.setattr("asyncio.get_running_loop", lambda: NoReaderLoop())
    monkeypatch.setattr("code_factory.application.service.sys.stdin", FakeStdin(""))
    await service._monitor_dashboard_input(stop_event)

    callbacks.clear()
    removed.clear()
    stop_event = asyncio.Event()
    monkeypatch.setattr("asyncio.get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr("code_factory.application.service.sys.stdin", FakeStdin(""))
    task = asyncio.create_task(service._monitor_dashboard_input(stop_event))
    await asyncio.sleep(0)
    callbacks[0]()
    await task
    assert stop_event.is_set() is True

    callbacks.clear()
    removed.clear()
    stop_event = asyncio.Event()
    monkeypatch.setattr("asyncio.get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(
        "code_factory.application.service.sys.stdin", FakeStdin("nope\n")
    )
    task = asyncio.create_task(service._monitor_dashboard_input(stop_event))
    await asyncio.sleep(0)
    callbacks[0]()
    assert stop_event.is_set() is False
    stop_event.set()
    await task

    recorded: list[Any] = []

    class SignalLoop:
        def add_signal_handler(self, sig: Any, callback: Any) -> None:
            recorded.append(sig)

    monkeypatch.setattr("asyncio.get_running_loop", lambda: SignalLoop())
    service._install_signal_handlers(asyncio.Event())
    assert recorded == [signal.SIGTERM]


def test_service_build_http_server_and_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    snapshot = make_snapshot(workflow)
    service = CodeFactoryService(str(workflow), port_override=4567)

    calls: list[tuple[str, int | None]] = []

    class FakeServer:
        def __init__(self, _orchestrator: Any, *, host: str, port: int) -> None:
            calls.append((host, port))

    monkeypatch.setattr(
        "code_factory.application.service.ObservabilityHTTPServer", FakeServer
    )
    server = service._build_http_server(snapshot, cast(Any, object()))
    assert isinstance(server, FakeServer)
    assert calls == [("127.0.0.1", 4567)]

    disabled_snapshot = make_snapshot(
        write_workflow_file(tmp_path / "NO_SERVER.md", server={"port": None})
    )
    disabled_service = CodeFactoryService(str(workflow))
    disabled_server = disabled_service._build_http_server(
        disabled_snapshot, cast(Any, object())
    )
    assert isinstance(disabled_server, FakeServer)
    assert calls[-1] == ("127.0.0.1", None)

    recorded: list[Any] = []

    class FakeLoop:
        def add_signal_handler(self, sig: Any, callback: Any) -> None:
            recorded.append(sig)
            raise NotImplementedError

    monkeypatch.setattr("asyncio.get_running_loop", lambda: FakeLoop())
    service._install_signal_handlers(asyncio.Event())
    assert recorded == [signal.SIGTERM]


def test_service_build_http_server_reraises_unexpected_constructor_typeerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md"))
    service = CodeFactoryService(str(snapshot.path))

    class BrokenServer:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise TypeError("boom")

    monkeypatch.setattr(
        "code_factory.application.service.ObservabilityHTTPServer", BrokenServer
    )
    with pytest.raises(TypeError, match="boom"):
        service._build_http_server(snapshot, cast(Any, object()))


@pytest.mark.parametrize(
    ("func", "value", "field", "expected"),
    [
        (require_mapping, None, "field", {}),
        (coerce_int, " 7 ", "field", 7),
        (positive_int, None, "field", 9),
        (non_negative_int, None, "field", 3),
        (optional_non_negative_int, None, "field", None),
        (optional_string, "value", "field", "value"),
        (string_with_default, None, "field", "fallback"),
        (required_command, "cmd", "field", "cmd"),
        (string_list, ["a", "b"], "field", ("x",)),
        (boolean, None, "field", True),
    ],
)
def test_config_utils_success_cases(
    func: Any, value: Any, field: str, expected: Any
) -> None:
    if func is positive_int:
        assert func(value, field, 9) == expected
    elif func is non_negative_int:
        assert func(value, field, 3) == expected
    elif func is string_with_default:
        assert func(value, field, "fallback") == expected
    elif func is required_command:
        assert func(value, field, "fallback") == expected
    elif func is string_list:
        assert func(value, field, ("x",)) == ("a", "b")
    elif func is boolean:
        assert func(value, field, True) is expected
    else:
        assert func(value, field) == expected


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: require_mapping([], "field"), "field must be an object"),
        (lambda: coerce_int(True, "field"), "field must be an integer"),
        (lambda: coerce_int(" ", "field"), "field must be an integer"),
        (lambda: positive_int(0, "field", 1), "field must be greater than 0"),
        (
            lambda: non_negative_int(-1, "field", 1),
            "field must be greater than or equal to 0",
        ),
        (lambda: optional_string(1, "field"), "field must be a string"),
        (lambda: string_with_default(1, "field", "x"), "field must be a string"),
        (lambda: required_command("", "field", "x"), "field can't be blank"),
        (
            lambda: string_list(["ok", 1], "field", ()),
            "field must be a list of strings",
        ),
        (
            lambda: normalize_state_limits({"": 1}, "field"),
            "field state names must not be blank",
        ),
        (
            lambda: normalize_state_limits({"Todo": 0}, "field"),
            "field limits must be positive integers",
        ),
        (lambda: boolean("yes", "field", False), "field must be a boolean"),
        (
            lambda: resolve_path_value(7, "/tmp/default", "field"),
            "field must be a string",
        ),
        (
            lambda: resolve_secret_setting(7, None, "field"),
            "field must be a string",
        ),
    ],
)
def test_config_utils_validation_errors(call: Any, message: str) -> None:
    with pytest.raises(ConfigValidationError, match=message):
        call()


def test_config_utils_environment_and_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", "/tmp/root")
    monkeypatch.setenv("SECRET_TOKEN", "")

    assert normalize_state_limits({"To Do": "2"}, "field") == {"to do": 2}
    assert boolean(False, "field", True) is False
    assert env_reference_name("$WORKSPACE_ROOT") == "WORKSPACE_ROOT"
    assert env_reference_name("$9bad") is None
    assert normalize_path_token("$WORKSPACE_ROOT") == "/tmp/root"
    assert normalize_path_token("literal") == "literal"
    assert resolve_path_value("$WORKSPACE_ROOT", "/tmp/default", "field") == "/tmp/root"
    assert normalize_secret_value("") is None
    assert resolve_env_value("$SECRET_TOKEN", "fallback") is None
    assert resolve_env_value("$MISSING_SECRET", "fallback") == "fallback"
    assert resolve_env_value("literal", "fallback") == "literal"
    assert resolve_secret_setting("$SECRET_TOKEN", "fallback", "field") is None
    assert normalize_keys({"x": [{"y": 1}]}) == {"x": [{"y": 1}]}


def test_validate_dispatch_settings_and_state_limit_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        agent={
            "max_concurrent_agents": 5,
            "max_concurrent_agents_by_state": {"Todo": 2},
        },
    )
    settings = make_snapshot(workflow).settings
    tracker_calls: list[Any] = []
    agent_calls: list[Any] = []

    monkeypatch.setattr(
        "code_factory.config.validation.validate_tracker_settings",
        lambda s: tracker_calls.append(s),
    )
    monkeypatch.setattr(
        "code_factory.config.validation.validate_coding_agent_settings",
        lambda s: agent_calls.append(s),
    )

    validate_dispatch_settings(settings)
    assert tracker_calls == [settings]
    assert agent_calls == [settings]
    assert max_concurrent_agents_for_state(settings, "Todo") == 2
    assert max_concurrent_agents_for_state(settings, "Done") == 5
    assert max_concurrent_agents_for_state(settings, None) == 5


def test_prompt_value_serialization() -> None:
    @dataclass
    class Payload:
        when: datetime
        on: date
        at: time
        tags: tuple[str, ...]

    result = to_liquid_value(
        Payload(
            when=datetime(2024, 1, 2, 3, 4, tzinfo=UTC),
            on=date(2024, 1, 2),
            at=time(3, 4),
            tags=("a", "b"),
        )
    )

    assert result == {
        "when": "2024-01-02T03:04:00Z",
        "on": "2024-01-02",
        "at": "03:04:00",
        "tags": ["a", "b"],
    }


def test_workflow_loader_helpers_and_current_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert workflow_file_path() == str(tmp_path / DEFAULT_WORKFLOW_FILENAME)
    assert workflow_file_path("custom.md") == "custom.md"
    assert split_front_matter("---\na: 1\n---\nbody\n") == (["a: 1"], ["body"])
    assert split_front_matter("---\na: 1\n") == (["a: 1"], [])
    assert front_matter_yaml_to_map([]) == {}
    assert front_matter_yaml_to_map(["null"]) == {}
    assert front_matter_yaml_to_map(["a: 1"]) == {"a": 1}
    with pytest.raises(WorkflowLoadError, match="workflow_parse_error"):
        front_matter_yaml_to_map(["a: ["])

    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("prompt\n", encoding="utf-8")
    stamp = current_stamp(str(workflow))
    assert stamp.size == workflow.stat().st_size
    with pytest.raises(WorkflowLoadError, match="missing_workflow_file"):
        current_stamp(str(tmp_path / "missing.md"))


def test_multi_state_workflow_helper_validation_paths() -> None:
    assert configured_active_states(
        {"states": {"Todo": {"prompt": "default"}}},
        {},
    ) == ("Todo",)
    assert parse_state_profiles({}, {}) == {}
    assert parse_prompt_sections(["", "# prompt: default", "Body"]) == {
        "default": "Body"
    }
    assert finalize_prompt_section({}, None, []) is None

    with pytest.raises(ConfigValidationError, match="states is required"):
        configured_active_states({}, {})
    with pytest.raises(ConfigValidationError, match="states must be an object"):
        configured_active_states({"states": []}, {})
    with pytest.raises(ConfigValidationError, match="states keys must not be blank"):
        configured_active_states({"states": {"   ": {"prompt": "default"}}}, {})
    with pytest.raises(
        ConfigValidationError, match="states must define at least one active state"
    ):
        configured_active_states({"states": {}}, {})
    with pytest.raises(WorkflowLoadError, match="workflow_prompt_section_name_blank"):
        parse_prompt_sections(["# prompt:   "])
    with pytest.raises(WorkflowLoadError, match="workflow_prompt_section_duplicate"):
        parse_prompt_sections(["# prompt: default", "One", "# prompt: default", "Two"])
    with pytest.raises(WorkflowLoadError, match="workflow_prompt_sections_missing"):
        parse_prompt_sections(["", "   "])


@pytest.mark.parametrize(
    ("config", "prompt_sections", "message"),
    [
        ({"states": []}, {"default": "Body"}, "states must be an object"),
        (
            {"states": {"Todo": {"prompt": "default"}}},
            {},
            "requires named `# prompt:` sections",
        ),
        (
            {"states": {"   ": {"prompt": "default"}}},
            {"default": "Body"},
            "states keys must not be blank",
        ),
        (
            {
                "states": {
                    "Todo": {"prompt": "default"},
                    " todo ": {"prompt": "default"},
                }
            },
            {"default": "Body"},
            "duplicate normalized state",
        ),
        (
            {"states": {"Todo": {"prompt": "default", "agent": {}}}},
            {"default": "Body"},
            "unsupported keys",
        ),
        (
            {"states": {"Todo": {"prompt": []}}},
            {"default": "Body"},
            "must not be empty",
        ),
        (
            {"states": {"Todo": {"prompt": 1}}},
            {"default": "Body"},
            "must be a string or list of strings",
        ),
        (
            {"states": {"Todo": {"prompt": [1]}}},
            {"default": "Body"},
            "must be a string or list of strings",
        ),
        (
            {"states": {"Todo": {"prompt": ["   "]}}},
            {"default": "Body"},
            "entries must not be blank",
        ),
        (
            {"states": {"Todo": {"prompt": "default", "codex": {"sandbox": "x"}}}},
            {"default": "Body"},
            "unsupported keys",
        ),
        (
            {"states": {"Todo": {"prompt": "default", "auto_next_state": "Done"}}},
            {"default": "Body"},
            "cannot define both prompt and auto_next_state",
        ),
        (
            {"states": {"Todo": {}}},
            {"default": "Body"},
            "must define either prompt or auto_next_state",
        ),
        (
            {"states": {"Todo": {"auto_next_state": "Done", "codex": {"model": "x"}}}},
            {"default": "Body"},
            "codex is not supported for auto states",
        ),
        (
            {"states": {"Todo": {"prompt": "default", "failure_state": " todo "}}},
            {"default": "Body"},
            "failure_state must not equal the current state",
        ),
        (
            {"states": {"Todo": {"prompt": "default", "allowed_next_states": "Done"}}},
            {"default": "Body"},
            "allowed_next_states must be a list of strings",
        ),
        (
            {"states": {"Todo": {"prompt": "default", "allowed_next_states": [1]}}},
            {"default": "Body"},
            "allowed_next_states must be a string",
        ),
        (
            {
                "states": {
                    "Todo": {
                        "prompt": "default",
                        "allowed_next_states": ["Done", " done "],
                    }
                }
            },
            {"default": "Body"},
            "must not contain duplicate normalized states",
        ),
        (
            {"states": {"Todo": {"prompt": "default", "failure_state": 1}}},
            {"default": "Body"},
            "failure_state must be a string",
        ),
        (
            {"states": {"Todo": {"auto_next_state": "   "}}},
            {"default": "Body"},
            "auto_next_state must not be blank",
        ),
    ],
)
def test_parse_state_profiles_validation_paths(
    config: dict[str, Any],
    prompt_sections: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ConfigValidationError, match=message):
        parse_state_profiles(config, prompt_sections)


def test_state_profiles_and_result_helpers_cover_edge_paths(tmp_path: Path) -> None:
    profiles = parse_state_profiles(
        {
            "states": {
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {
                    "prompt": "default",
                    "allowed_next_states": ["Human Review"],
                    "failure_state": "Blocked",
                    "codex": {"reasoning_effort": "high"},
                },
            }
        },
        {"default": "Body"},
    )
    todo_profile = profiles["todo"]
    progress_profile = profiles["in progress"]
    assert todo_profile.is_auto is True
    assert todo_profile.is_agent_run is False
    assert progress_profile.is_agent_run is True
    assert progress_profile.codex_model("gpt-5.4") == "gpt-5.4"
    assert progress_profile.codex_reasoning_effort("low") == "high"
    assert progress_profile.allows_next_state("Human Review") is True
    assert progress_profile.allows_next_state("Done") is False
    assert structured_turn_output_schema(("Done", "Review"))["properties"][
        "next_state"
    ] == {"enum": ["Done", "Review", None]}
    assert structured_turn_output_schema()["properties"]["next_state"] == {
        "type": ["string", "null"]
    }

    rendered = render_result_comment(
        "Review",
        StructuredTurnResult(
            decision="transition",
            summary="Completed review\nWith notes",
            next_state="Done",
        ),
    )
    parsed = parse_result_comment(rendered)
    assert parsed is not None
    assert parsed[0] == "Review"
    assert parsed[1].summary == "Completed review\nWith notes"
    assert normalize_structured_turn_result(
        {"decision": "blocked", "summary": "  waiting  "}
    ) == StructuredTurnResult(decision="blocked", summary="waiting", next_state=None)
    assert normalize_structured_turn_result(
        {"decision": "transition", "summary": "done"}
    ) == StructuredTurnResult(decision="transition", summary="done", next_state=None)
    assert normalize_structured_turn_result([]) is None
    assert (
        normalize_structured_turn_result({"decision": "nope", "summary": "x"}) is None
    )
    assert (
        normalize_structured_turn_result({"decision": "transition", "summary": " "})
        is None
    )
    assert (
        normalize_structured_turn_result(
            {"decision": "transition", "summary": "x", "next_state": ""}
        )
        is None
    )
    assert parse_result_comment(None) is None
    assert parse_result_comment("not a result comment") is None
    assert parse_result_comment("## Code Factory Result: \n\nversion: 1\n") is None
    assert parse_result_comment("## Code Factory Result: Review\n\n- nope\n") is None
    assert (
        parse_result_comment(
            "## Code Factory Result: Review\n\nversion: 2\ndecision: transition\nsummary: |\n  x\n"
        )
        is None
    )
    assert (
        parse_result_comment(
            "## Code Factory Result: Review\n\nversion: 1\ndecision: invalid\nsummary: |\n  x\n"
        )
        is None
    )

    snapshot = make_snapshot(
        write_workflow_file(
            tmp_path / "WORKFLOW.md",
            states={
                "Todo": {"auto_next_state": "In Progress"},
                "In Progress": {"prompt": "default"},
            },
        )
    )
    todo_profile = snapshot.state_profile("Todo")
    assert todo_profile is not None
    assert todo_profile.auto_next_state == "In Progress"


@pytest.mark.asyncio
async def test_workflow_store_reload_run_and_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", prompt="one")
    snapshots: list[Any] = []
    errors: list[Any] = []
    actor = WorkflowStoreActor(
        str(workflow),
        on_snapshot=lambda snapshot: snapshots.append(snapshot) or asyncio.sleep(0),
        on_error=lambda error: errors.append(error) or asyncio.sleep(0),
        poll_interval_s=0.001,
    )

    initial = await actor.load_initial_snapshot()
    assert initial.version == 1

    await actor.reload_if_changed()
    assert snapshots == []

    workflow.write_text(
        "---\ntracker:\n  kind: linear\n  api_key: token\n  project_slug: next\n"
        "states:\n  Todo:\n    prompt: default\n---\n# prompt: default\nnew\n",
        encoding="utf-8",
    )
    await actor.reload_if_changed()
    assert snapshots[-1].version == 2

    monkeypatch.setattr(
        "code_factory.workflow.store.current_stamp",
        lambda _path: (_ for _ in ()).throw(OSError("boom")),
    )
    await actor.reload_if_changed()
    assert actor._state is not None
    assert actor._state.last_reload_error is not None
    assert errors

    stop_event = asyncio.Event()
    runs = 0

    async def fake_reload() -> None:
        nonlocal runs
        runs += 1
        stop_event.set()

    actor.reload_if_changed = fake_reload  # type: ignore[method-assign]
    await actor.run(stop_event)
    assert runs == 1


@pytest.mark.asyncio
async def test_workflow_store_current_snapshot_and_watch_loop_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    actor = WorkflowStoreActor(str(workflow))
    actor.subscribe(on_snapshot=lambda snapshot: asyncio.sleep(0))
    actor.subscribe(on_error=lambda error: asyncio.sleep(0))
    initial = await actor.load_initial_snapshot()
    assert actor.current_snapshot() == initial

    stop_event = asyncio.Event()

    async def fake_awatch(*_args: Any, **_kwargs: Any):
        yield {("modified", str(workflow))}

    async def fake_reload() -> Any:
        stop_event.set()
        return actor.current_snapshot()

    monkeypatch.setattr("code_factory.workflow.store.awatch", fake_awatch)
    actor.reload_if_changed = fake_reload  # type: ignore[method-assign]
    await actor._watch_loop(stop_event)


@pytest.mark.asyncio
async def test_service_run_forever_wires_runtime_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md", server={"port": 4321})
    snapshot = make_snapshot(workflow)
    service = CodeFactoryService(str(workflow), logs_root=str(tmp_path / "logs"))
    calls: list[str] = []

    class FakeOrchestrator:
        def __init__(self, initial_snapshot: Any) -> None:
            self.initial_snapshot = initial_snapshot

        async def startup_terminal_workspace_cleanup(self) -> None:
            calls.append("cleanup")

        async def run(self, stop_event: asyncio.Event) -> None:
            calls.append("orchestrator.run")
            stop_event.set()

        async def shutdown(self) -> None:
            calls.append("orchestrator.shutdown")

        async def notify_workflow_updated(self, snapshot: Any) -> None:
            calls.append("workflow.updated")

        async def notify_workflow_reload_error(self, error: Any) -> None:
            calls.append("workflow.error")

    class FakeWorkflowStoreActor:
        def __init__(
            self, path: str, *, on_snapshot: Any, on_error: Any = None
        ) -> None:
            self.path = path
            self.on_snapshot = on_snapshot
            self.on_error = on_error

        async def load_initial_snapshot(self) -> Any:
            calls.append("workflow.load")
            return snapshot

        async def run(self, stop_event: asyncio.Event) -> None:
            calls.append("workflow.run")
            await stop_event.wait()

    class FakeHTTPServer:
        def __init__(self, orchestrator: Any, *, host: str, port: int) -> None:
            self.host = host
            self.port = port

        async def run(self, stop_event: asyncio.Event) -> None:
            calls.append("http.run")
            await stop_event.wait()

    monkeypatch.setattr(
        "code_factory.application.service.WorkflowStoreActor", FakeWorkflowStoreActor
    )
    monkeypatch.setattr(
        "code_factory.application.service.OrchestratorActor", FakeOrchestrator
    )
    monkeypatch.setattr(
        "code_factory.application.service.ObservabilityHTTPServer", FakeHTTPServer
    )
    monkeypatch.setattr(
        "code_factory.application.service.configure_logging",
        lambda logs_root: Path(logs_root) / "log" / "code-factory.log",
    )
    monkeypatch.setattr(
        "code_factory.application.service.validate_dispatch_settings",
        lambda settings: calls.append("validate"),
    )
    monkeypatch.setattr(
        service, "_install_signal_handlers", lambda stop_event: calls.append("signals")
    )

    await service.run_forever()

    assert calls == [
        "signals",
        "workflow.load",
        "validate",
        "cleanup",
        "orchestrator.run",
        "workflow.run",
        "http.run",
        "orchestrator.shutdown",
    ]
    assert service.orchestrator is not None
    assert service.workflow_store is not None


@pytest.mark.asyncio
async def test_service_run_forever_reraises_unexpected_orchestrator_typeerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    snapshot = make_snapshot(workflow)
    service = CodeFactoryService(str(workflow))

    class FakeWorkflowStoreActor:
        def __init__(
            self, path: str, *, on_snapshot: Any, on_error: Any = None
        ) -> None:
            return None

        async def load_initial_snapshot(self) -> Any:
            return snapshot

    class BrokenOrchestrator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise TypeError("boom")

    monkeypatch.setattr(
        "code_factory.application.service.WorkflowStoreActor", FakeWorkflowStoreActor
    )
    monkeypatch.setattr(
        "code_factory.application.service.OrchestratorActor", BrokenOrchestrator
    )
    monkeypatch.setattr(
        "code_factory.application.service.validate_dispatch_settings",
        lambda settings: None,
    )
    monkeypatch.setattr(service, "_install_signal_handlers", lambda stop_event: None)
    monkeypatch.setattr(
        service, "_configure_logging", lambda dashboard_enabled, diagnostics=None: None
    )

    with pytest.raises(TypeError, match="boom"):
        await service.run_forever()


@pytest.mark.asyncio
async def test_service_run_forever_starts_dashboard_and_input_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(tmp_path / "WORKFLOW.md")
    snapshot = make_snapshot(workflow)
    service = CodeFactoryService(str(workflow))
    calls: list[str] = []

    class FakeOrchestrator:
        def __init__(self, initial_snapshot: Any) -> None:
            self.initial_snapshot = initial_snapshot

        async def startup_terminal_workspace_cleanup(self) -> None:
            return None

        async def run(self, stop_event: asyncio.Event) -> None:
            await stop_event.wait()

        async def shutdown(self) -> None:
            calls.append("shutdown")

        async def notify_workflow_updated(self, snapshot: Any) -> None:
            return None

        async def notify_workflow_reload_error(self, error: Any) -> None:
            return None

    class FakeWorkflowStoreActor:
        def __init__(
            self, path: str, *, on_snapshot: Any, on_error: Any = None
        ) -> None:
            return None

        async def load_initial_snapshot(self) -> Any:
            return snapshot

        async def run(self, stop_event: asyncio.Event) -> None:
            await stop_event.wait()

    class FakeDashboard:
        async def run(self, stop_event: asyncio.Event) -> None:
            calls.append("dashboard.run")
            await stop_event.wait()

    monkeypatch.setattr(
        "code_factory.application.service.WorkflowStoreActor", FakeWorkflowStoreActor
    )
    monkeypatch.setattr(
        "code_factory.application.service.OrchestratorActor", FakeOrchestrator
    )
    monkeypatch.setattr(
        "code_factory.application.service.validate_dispatch_settings",
        lambda settings: None,
    )
    monkeypatch.setattr(service, "_install_signal_handlers", lambda stop_event: None)
    monkeypatch.setattr(
        service, "_configure_logging", lambda dashboard_enabled, diagnostics=None: None
    )
    monkeypatch.setattr(service, "_build_http_server", lambda *args: None)
    monkeypatch.setattr(
        service, "_build_status_dashboard", lambda *args: FakeDashboard()
    )
    monkeypatch.setattr(service, "_dashboard_input_supported", lambda: True)

    async def fake_monitor(stop_event: asyncio.Event) -> None:
        calls.append("monitor.run")
        stop_event.set()

    monkeypatch.setattr(service, "_monitor_dashboard_input", fake_monitor)
    await service.run_forever()
    assert calls == ["dashboard.run", "monitor.run", "shutdown"]
    assert await service._ignore_snapshot(None) is None


def test_workspace_path_helpers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_path = root / "escape"
    symlink_path.symlink_to(outside, target_is_directory=True)

    assert canonicalize(str(root / "a" / ".." / "a" / "b")) == str(nested.resolve())
    assert is_within(str(root), str(nested))
    assert not is_within(str(root), str(outside))
    assert safe_identifier("A/B:C") == "A_B_C"
    assert workspace_path_for_issue(str(root), "A/B") == str((root / "A_B").resolve())
    assert validate_workspace_path(str(root), str(nested)) == str(nested.resolve())
    with pytest.raises(WorkspaceError, match="workspace_equals_root"):
        validate_workspace_path(str(root), str(root))
    with pytest.raises(WorkspaceError, match="workspace_outside_root"):
        validate_workspace_path(str(root), str(outside))
    with pytest.raises(WorkspaceError, match="workspace_symlink_escape"):
        validate_workspace_path(str(root), str(symlink_path))


def test_workspace_utils_and_issue_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    created = ensure_workspace(str(workspace))
    assert created is True
    file_path = tmp_path / "workspace-file"
    file_path.write_text("x", encoding="utf-8")
    assert ensure_workspace(str(file_path)) is True
    assert file_path.is_dir()

    tmp_dir = workspace / "tmp"
    elixir_ls = workspace / ".elixir_ls"
    tmp_dir.mkdir(parents=True)
    elixir_ls.mkdir()
    clean_tmp_artifacts(str(workspace))
    assert not tmp_dir.exists()
    assert not elixir_ls.exists()

    issue = make_issue(id="i-1", identifier="MT-9")
    assert issue_context(issue) == {"issue_id": "i-1", "issue_identifier": "MT-9"}
    assert issue_context("MT-2") == {"issue_id": None, "issue_identifier": "MT-2"}
    assert issue_context(None) == {"issue_id": None, "issue_identifier": "issue"}


@pytest.mark.asyncio
async def test_run_hook_handles_success_timeout_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_snapshot(write_workflow_file(tmp_path / "WORKFLOW.md")).settings
    calls: list[Any] = []

    class FakeProcess:
        async def capture_output(self, timeout_ms: int) -> tuple[int, str]:
            calls.append(("capture", timeout_ms))
            return 0, "ok"

        async def terminate(self) -> None:
            calls.append("terminate")

    async def spawn_success(*args: Any, **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(
        "code_factory.workspace.hooks.ProcessTree.spawn_shell", spawn_success
    )
    await run_hook(
        settings,
        "echo ok",
        str(tmp_path),
        {"issue_id": "i-1", "issue_identifier": "MT-1"},
        "before_run",
        fatal=True,
    )

    class TimeoutProcess(FakeProcess):
        async def capture_output(self, timeout_ms: int) -> tuple[int, str]:
            raise TimeoutError

    async def spawn_timeout(*args: Any, **kwargs: Any) -> TimeoutProcess:
        return TimeoutProcess()

    monkeypatch.setattr(
        "code_factory.workspace.hooks.ProcessTree.spawn_shell", spawn_timeout
    )
    with pytest.raises(WorkspaceError, match="workspace_hook_timeout"):
        await run_hook(
            settings,
            "sleep",
            str(tmp_path),
            {"issue_id": "i-1", "issue_identifier": "MT-1"},
            "before_run",
            fatal=True,
        )

    class FailureProcess(FakeProcess):
        async def capture_output(self, timeout_ms: int) -> tuple[int, str]:
            return 17, "boom"

    async def spawn_failure(*args: Any, **kwargs: Any) -> FailureProcess:
        return FailureProcess()

    monkeypatch.setattr(
        "code_factory.workspace.hooks.ProcessTree.spawn_shell", spawn_failure
    )
    with pytest.raises(WorkspaceError, match="workspace_hook_failed"):
        await run_hook(
            settings,
            "false",
            str(tmp_path),
            {"issue_id": "i-1", "issue_identifier": "MT-1"},
            "after_run",
            fatal=False,
        )


@pytest.mark.asyncio
async def test_workspace_manager_runs_hooks_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        workspace={"root": str(tmp_path / "workspaces")},
        hooks={
            "after_create": "create",
            "before_run": "before",
            "after_run": "after",
            "before_remove": "remove",
            "timeout_ms": 1000,
        },
    )
    snapshot = make_snapshot(workflow)
    manager = WorkspaceManager(snapshot.settings)
    calls: list[tuple[str, str]] = []

    async def fake_run_hook(
        _settings: Any,
        command: str,
        workspace: str,
        _issue_context: Any,
        hook_name: str,
        *,
        fatal: bool,
    ) -> None:
        calls.append((hook_name, command))
        if hook_name in {"after_run", "before_remove"} and not fatal:
            raise WorkspaceError(("workspace_hook_failed", hook_name, 1, "boom"))

    monkeypatch.setattr("code_factory.workspace.manager.run_hook", fake_run_hook)
    workspace = await manager.create_for_issue("MT/42")
    assert workspace.created_now is True
    assert workspace.workspace_key == "MT_42"

    reused = await manager.create_for_issue("MT/42")
    assert reused.created_now is False

    await manager.run_before_run_hook(workspace.path, "MT/42")
    await manager.run_after_run_hook(workspace.path, "MT/42")
    await manager.remove_issue_workspaces(None)
    await manager.remove_issue_workspaces("MT/42")

    doomed = Path(workspace.path)
    doomed.mkdir(parents=True, exist_ok=True)
    await manager.remove(workspace.path)
    assert not doomed.exists()
    assert ("after_create", "create") in calls
    assert ("before_run", "before") in calls
    assert ("after_run", "after") in calls
    assert ("before_remove", "remove") in calls


@pytest.mark.asyncio
async def test_workspace_manager_removes_failed_after_create_workspace_and_retries(
    tmp_path: Path,
) -> None:
    attempts_file = tmp_path / "attempts.txt"
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md",
        workspace={"root": str(tmp_path / "workspaces")},
        hooks={
            "after_create": "\n".join(
                (
                    f"count=$(cat {shlex.quote(str(attempts_file))} 2>/dev/null || echo 0)",
                    "count=$((count + 1))",
                    f"printf '%s' \"$count\" > {shlex.quote(str(attempts_file))}",
                    'if [ "$count" -eq 1 ]; then',
                    '  printf "bootstrap failed on attempt %s\\n" "$count"',
                    "  exit 17",
                    "fi",
                    'printf "bootstrap succeeded on attempt %s\\n" "$count"',
                )
            ),
            "timeout_ms": 1_000,
        },
    )
    snapshot = make_snapshot(workflow)
    manager = WorkspaceManager(snapshot.settings)
    workspace_path = Path(manager.workspace_path_for_issue("MT-77"))

    with pytest.raises(WorkspaceError, match="workspace_hook_failed"):
        await manager.create_for_issue("MT-77")

    assert attempts_file.read_text(encoding="utf-8") == "1"
    assert workspace_path.exists() is False

    workspace = await manager.create_for_issue("MT-77")
    assert workspace.created_now is True
    assert workspace.path == str(workspace_path)
    assert attempts_file.read_text(encoding="utf-8") == "2"


def test_workspace_manager_failed_workspace_cleanup_logs_removal_problems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workflow = write_workflow_file(
        tmp_path / "WORKFLOW.md", workspace={"root": str(tmp_path / "workspaces")}
    )
    snapshot = make_snapshot(workflow)
    manager = WorkspaceManager(snapshot.settings)

    monkeypatch.setattr(
        "code_factory.workspace.manager.shutil.rmtree",
        lambda path, ignore_errors=False: (_ for _ in ()).throw(FileNotFoundError()),
    )
    manager._remove_failed_new_workspace(str(tmp_path / "missing"), None)

    def fail_remove(path: str, ignore_errors: bool = False) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("code_factory.workspace.manager.shutil.rmtree", fail_remove)
    with caplog.at_level(logging.WARNING):
        manager._remove_failed_new_workspace(str(tmp_path / "broken"), None)
    assert "Failed to remove partially created workspace" in caplog.text


@pytest.mark.asyncio
async def test_maybe_aclose_supports_sync_async_and_missing_close() -> None:
    events: list[str] = []

    class AsyncCloser:
        async def close(self) -> None:
            events.append("async")

    class SyncCloser:
        def close(self) -> None:
            events.append("sync")

    await maybe_aclose(AsyncCloser())
    await maybe_aclose(SyncCloser())
    await maybe_aclose(object())
    assert events == ["async", "sync"]


def test_issue_helpers() -> None:
    issue = Issue(labels=("backend", "ops"))
    assert issue.label_names() == ["backend", "ops"]
    assert normalize_issue_state(" In Progress ") == "in progress"
    assert normalize_issue_state(None) == ""
