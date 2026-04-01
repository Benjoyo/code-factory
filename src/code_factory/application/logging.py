"""Logging bootstrap helpers shared by the Code Factory service entrypoints."""

from __future__ import annotations

import logging
from logging import NullHandler
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .dashboard.dashboard_diagnostics import (
    DashboardDiagnostics,
    DashboardDiagnosticsHandler,
)


def configure_logging(
    logs_root: str | None,
    *,
    console: bool = True,
    diagnostics: DashboardDiagnostics | None = None,
) -> Path | None:
    """Set up shared handlers once and return the persistent log file path if enabled."""

    root_logger = logging.getLogger()
    log_path = _log_path(logs_root)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        if console:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            root_logger.addHandler(stream_handler)
        elif logs_root is None and diagnostics is None:
            root_logger.addHandler(NullHandler())
    if log_path is not None:
        _install_rotating_file_handler(root_logger, log_path, formatter)
    _install_diagnostics_handler(root_logger, diagnostics)
    configure_library_loggers()
    return log_path


def configure_library_loggers() -> None:
    """Quiet noisy third-party loggers so they do not overwhelm runtime logs."""

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def _install_diagnostics_handler(
    root_logger: logging.Logger, diagnostics: DashboardDiagnostics | None
) -> None:
    if diagnostics is None:
        return
    for handler in root_logger.handlers:
        if (
            isinstance(handler, DashboardDiagnosticsHandler)
            and handler.diagnostics is diagnostics
        ):
            return
    root_logger.addHandler(DashboardDiagnosticsHandler(diagnostics))


def _install_rotating_file_handler(
    root_logger: logging.Logger,
    log_path: Path,
    formatter: logging.Formatter,
) -> None:
    for handler in root_logger.handlers:
        if _handler_targets_path(handler, log_path):
            return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def _handler_targets_path(handler: logging.Handler, log_path: Path) -> bool:
    current_path = getattr(handler, "baseFilename", None) or getattr(
        handler, "path", None
    )
    return Path(current_path).resolve() == log_path if current_path else False


def _log_path(logs_root: str | None) -> Path | None:
    if logs_root is None:
        return None
    return Path(logs_root).expanduser().resolve() / "log" / "code-factory.log"
