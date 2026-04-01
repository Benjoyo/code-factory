"""Shared Linear-backed ticket operations used by tools and CLI surfaces."""

from __future__ import annotations

from .ops_write import LinearOpsWriteMixin


class LinearOps(LinearOpsWriteMixin):
    """Normalized ticket operations backed by Linear GraphQL."""
