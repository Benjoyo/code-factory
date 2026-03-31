"""Port preflight helpers for review server launches."""

from __future__ import annotations

import socket
import subprocess
from urllib.parse import urlparse

from ..errors import ReviewError
from .review_models import ReviewTarget


def ensure_review_port_available(target: ReviewTarget, launch) -> None:
    if launch.port is None:
        return
    host = review_host(launch.url)
    if not review_port_in_use(host, launch.port):
        return
    details = review_port_owner_details(launch.port)
    suffix = f" {details}" if details is not None else ""
    raise ReviewError(
        f"{target.target}:{launch.name} can't start because port {launch.port} "
        f"on {host} is already in use.{suffix}"
    )


def review_host(url: str | None) -> str:
    if url is None:
        return "127.0.0.1"
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1"


def review_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.1)
        return sock.connect_ex((host, port)) == 0


def review_port_owner_details(port: int) -> str | None:
    pid = _listening_pid(port)
    if pid is None:
        return None
    command = _process_command(pid)
    if command is None:
        return f"Occupying PID: {pid}."
    return f"Occupying PID: {pid} (`{command}`)."


def _listening_pid(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].strip().isdigit():
            return int(line[1:].strip())
    return None


def _process_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None
