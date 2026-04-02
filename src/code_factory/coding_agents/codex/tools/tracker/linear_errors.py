"""Compatibility wrapper for shared Linear tracker error payload helpers."""

from __future__ import annotations

from .....trackers.user_errors import tracker_error_payload

linear_error_payload = tracker_error_payload

__all__ = ["linear_error_payload"]
