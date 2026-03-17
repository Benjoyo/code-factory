"""Logging bootstrap helpers shared by the Code Factory service entrypoints."""

from __future__ import annotations

import logging
from logging import NullHandler
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(logs_root: str | None, *, console: bool = True) -> Path | None:
    """Set up shared handlers once and return the persistent log file path if enabled."""

    root_logger = logging.getLogger()
    log_path: Path | None = None
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        if console:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            root_logger.addHandler(stream_handler)
        elif logs_root is None:
            root_logger.addHandler(NullHandler())
        if logs_root is not None:
            log_path = (
                Path(logs_root).expanduser().resolve() / "log" / "code-factory.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path, maxBytes=10 * 1024 * 1024, backupCount=5
            )
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
    elif logs_root is not None:
        # Preserve a reference to the configured log file even when handlers already exist.
        log_path = Path(logs_root).expanduser().resolve() / "log" / "code-factory.log"
    configure_library_loggers()
    return log_path


def configure_library_loggers() -> None:
    """Quiet noisy third-party loggers so they do not overwhelm runtime logs."""

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
