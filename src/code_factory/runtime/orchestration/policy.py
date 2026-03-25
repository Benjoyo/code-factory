"""Dispatch policy helpers that gate orchestrator concurrency and retries."""

from __future__ import annotations

from ...config import max_concurrent_agents_for_state
from ...config.models import Settings
from ...issues import Issue, normalize_issue_state
from .models import RunningEntry


def sort_issues_for_dispatch(issues: list[Issue]) -> list[Issue]:
    """Order candidates so high-priority, older issues dispatch first."""
    return sorted(issues, key=dispatch_sort_key)


def dispatch_sort_key(issue: Issue) -> tuple[int, int, str]:
    """Return the tuple used to prioritize issues within `sort_issues_for_dispatch`."""
    priority = (
        issue.priority
        if isinstance(issue.priority, int) and 1 <= issue.priority <= 4
        else 5
    )
    created_at = (
        int(issue.created_at.timestamp() * 1_000_000)
        if issue.created_at
        else 9_223_372_036_854_775_807
    )
    identifier = issue.identifier or issue.id or ""
    return priority, created_at, identifier


def candidate_issue(settings: Settings, issue: Issue) -> bool:
    """Return True when an issue has the minimal metadata and workflow state to run."""
    return bool(
        issue.id
        and issue.identifier
        and issue.title
        and issue.state
        and issue.assigned_to_worker
        and active_issue_state(settings, issue.state)
        and not terminal_issue_state(settings, issue.state)
    )


def todo_issue_blocked_by_non_terminal(settings: Settings, issue: Issue) -> bool:
    """Detect when a TODO issue is waiting on blockers that are not terminal."""
    if normalize_issue_state(issue.state) != "todo":
        return False
    return any(
        not terminal_issue_state(settings, blocker.state)
        for blocker in issue.blocked_by
    )


def terminal_issue_state(settings: Settings, state_name: str | None) -> bool:
    """Identify whether a state belongs to the workflow's terminal set."""
    return normalize_issue_state(state_name) in {
        normalize_issue_state(state) for state in settings.terminal_states
    }


def active_issue_state(settings: Settings, state_name: str | None) -> bool:
    """Identify whether a state belongs to the workflow's active set."""
    return normalize_issue_state(state_name) in {
        normalize_issue_state(state) for state in settings.tracker.active_states
    }


def state_slots_available(
    settings: Settings, running: dict[str, RunningEntry], issue: Issue
) -> bool:
    """Ensure per-state concurrency limits are not exceeded by this issue."""
    limit = max_concurrent_agents_for_state(settings, issue.state)
    normalized_state = normalize_issue_state(issue.state)
    used = sum(
        1
        for entry in running.values()
        if normalize_issue_state(entry.issue.state) == normalized_state
    )
    return limit > used


def available_slots(settings: Settings, running: dict[str, RunningEntry]) -> int:
    """Compute how many additional agents the orchestrator can start globally."""
    return max(settings.agent.max_concurrent_agents - len(running), 0)


def normalize_retry_attempt(attempt: int | None) -> int:
    """Normalize retry attempts so missing/invalid inputs default to zero."""
    return attempt if isinstance(attempt, int) and attempt > 0 else 0


def next_retry_attempt(entry: RunningEntry) -> int:
    """Increment the retry counter for an entry that just stopped."""
    return entry.retry_attempt + 1 if entry.retry_attempt > 0 else 1


def failure_retry_delay(base_ms: int, max_backoff_ms: int, attempt: int) -> int:
    """Compute exponential backoff bounded by the workflow max backoff setting."""
    # Cap the exponent to keep the delay within reasonable magnitudes.
    max_delay_power = min(attempt - 1, 10)
    return min(base_ms * (2**max_delay_power), max_backoff_ms)
