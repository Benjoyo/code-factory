from __future__ import annotations

from ...config import max_concurrent_agents_for_state
from ...config.models import Settings
from ...issues import Issue, normalize_issue_state
from .models import RunningEntry


def sort_issues_for_dispatch(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=dispatch_sort_key)


def dispatch_sort_key(issue: Issue) -> tuple[int, int, str]:
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
    if normalize_issue_state(issue.state) != "todo":
        return False
    return any(
        not terminal_issue_state(settings, blocker.state)
        for blocker in issue.blocked_by
    )


def terminal_issue_state(settings: Settings, state_name: str | None) -> bool:
    return normalize_issue_state(state_name) in {
        normalize_issue_state(state) for state in settings.tracker.terminal_states
    }


def active_issue_state(settings: Settings, state_name: str | None) -> bool:
    return normalize_issue_state(state_name) in {
        normalize_issue_state(state) for state in settings.tracker.active_states
    }


def state_slots_available(
    settings: Settings, running: dict[str, RunningEntry], issue: Issue
) -> bool:
    limit = max_concurrent_agents_for_state(settings, issue.state)
    normalized_state = normalize_issue_state(issue.state)
    used = sum(
        1
        for entry in running.values()
        if normalize_issue_state(entry.issue.state) == normalized_state
    )
    return limit > used


def available_slots(settings: Settings, running: dict[str, RunningEntry]) -> int:
    return max(settings.agent.max_concurrent_agents - len(running), 0)


def normalize_retry_attempt(attempt: int | None) -> int:
    return attempt if isinstance(attempt, int) and attempt > 0 else 0


def next_retry_attempt(entry: RunningEntry) -> int:
    return entry.retry_attempt + 1 if entry.retry_attempt > 0 else 1


def failure_retry_delay(base_ms: int, max_backoff_ms: int, attempt: int) -> int:
    max_delay_power = min(attempt - 1, 10)
    return min(base_ms * (2**max_delay_power), max_backoff_ms)
