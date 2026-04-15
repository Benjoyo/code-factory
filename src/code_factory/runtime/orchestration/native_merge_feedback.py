"""Review-feedback filters shared by the native merge fast path."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

CODEX_BOTS = {
    "chatgpt-codex-connector[bot]",
    "github-actions[bot]",
    "codex-gc-app[bot]",
    "app/codex-gc-app",
}
_MIN_TIME = datetime.min.replace(tzinfo=UTC)


def blocking_feedback_error(
    issue_comments: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> str | None:
    review_requested_at = _latest_review_request_at(issue_comments)
    if (
        _filter_human_issue_comments(issue_comments)
        or _filter_human_review_comments(review_comments)
        or _filter_codex_review_issue_comments(issue_comments)
    ):
        return "PR has unresolved review comments"
    if _filter_blocking_reviews(reviews, review_requested_at):
        return "PR has unresolved blocking review states"
    if _filter_codex_comments(
        issue_comments, review_requested_at
    ) or _filter_codex_comments(review_comments, review_requested_at):
        return "PR has unacknowledged Codex review comments"
    return None


def _latest_review_request_at(comments: list[dict[str, Any]]) -> datetime | None:
    timestamps = [
        _comment_time(comment)
        for comment in comments
        if not _is_codex_bot_user(comment.get("user", {}))
        and "@codex review" in str(comment.get("body") or "")
    ]
    return max((ts for ts in timestamps if ts is not None), default=None)


def _filter_human_issue_comments(
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_ack = _latest_codex_issue_reply_time(comments)
    return [
        comment
        for comment in comments
        if not _is_bot_user(comment.get("user", {}))
        and not _is_codex_reply_body(str(comment.get("body") or "").strip())
        and not _is_codex_review_body(str(comment.get("body") or "").strip())
        and "@codex review" not in str(comment.get("body") or "")
        and not _comment_acknowledged(comment, latest_ack)
    ]


def _filter_codex_review_issue_comments(
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_ack = _latest_codex_issue_reply_time(comments)
    return [
        comment
        for comment in comments
        if _is_codex_review_body(str(comment.get("body") or "").strip())
        and not _comment_acknowledged(comment, latest_ack)
    ]


def _filter_human_review_comments(
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_reply = _latest_codex_reply_by_thread(comments)
    filtered: list[dict[str, Any]] = []
    for comment in comments:
        if _is_bot_user(comment.get("user", {})):
            continue
        last_reply = latest_reply.get(_thread_root_id(comment))
        created_time = _comment_time(comment)
        if last_reply and created_time and created_time <= last_reply:
            continue
        filtered.append(comment)
    return filtered


def _filter_codex_comments(
    comments: list[dict[str, Any]], review_requested_at: datetime | None
) -> list[dict[str, Any]]:
    latest_reply = _latest_codex_reply_by_thread(comments)
    latest_issue_ack = _latest_codex_issue_reply_time(comments)
    filtered: list[dict[str, Any]] = []
    for comment in comments:
        if not _is_codex_bot_user(comment.get("user", {})):
            continue
        created_time = _comment_time(comment)
        if created_time is None or (
            review_requested_at is not None and created_time <= review_requested_at
        ):
            continue
        if comment.get("in_reply_to_id") or comment.get("pull_request_review_id"):
            last_reply = latest_reply.get(_thread_root_id(comment))
            if last_reply and created_time <= last_reply:
                continue
        elif latest_issue_ack is not None and created_time <= latest_issue_ack:
            continue
        filtered.append(comment)
    return filtered


def _filter_blocking_reviews(
    reviews: list[dict[str, Any]], review_requested_at: datetime | None
) -> list[dict[str, Any]]:
    latest_by_user: dict[str, dict[str, Any]] = {}
    for review in reviews:
        user_login = str((review.get("user") or {}).get("login") or "")
        if not user_login:
            continue
        current = latest_by_user.get(user_login)
        if current is None or _review_timestamp(review) > _review_timestamp(current):
            latest_by_user[user_login] = review
    return [
        review
        for review in latest_by_user.values()
        if _is_blocking_review(review, review_requested_at)
    ]


def _is_blocking_review(
    review: dict[str, Any], review_requested_at: datetime | None
) -> bool:
    created_time = _review_timestamp(review)
    user_login = str((review.get("user") or {}).get("login") or "")
    body = str(review.get("body") or "").strip()
    state = review.get("state")
    if (
        user_login in CODEX_BOTS
        and review_requested_at is not None
        and created_time <= review_requested_at
    ):
        return False
    if user_login in CODEX_BOTS:
        return state == "CHANGES_REQUESTED"
    if body.startswith("[codex]") or state in ("APPROVED", "DISMISSED"):
        return False
    if body or state == "CHANGES_REQUESTED":
        return True
    if state == "COMMENTED":
        return False
    return bool(state)


def _latest_codex_reply_by_thread(
    comments: list[dict[str, Any]],
) -> dict[int | None, datetime]:
    latest: dict[int | None, datetime] = {}
    for comment in comments:
        if not _is_codex_reply_body(str(comment.get("body") or "").strip()):
            continue
        root_id = _thread_root_id(comment)
        created_time = _comment_time(comment)
        if created_time is not None and created_time > latest.get(root_id, _MIN_TIME):
            latest[root_id] = created_time
    return latest


def _latest_codex_issue_reply_time(comments: list[dict[str, Any]]) -> datetime | None:
    timestamps = [
        _comment_time(comment)
        for comment in comments
        if _is_codex_reply_body(str(comment.get("body") or "").strip())
    ]
    return max((ts for ts in timestamps if ts is not None), default=None)


def _comment_acknowledged(comment: dict[str, Any], latest_ack: datetime | None) -> bool:
    created_time = _comment_time(comment)
    return (
        latest_ack is not None
        and created_time is not None
        and created_time <= latest_ack
    )


def _comment_time(comment: dict[str, Any]) -> datetime | None:
    timestamp = comment.get("updated_at") or comment.get("created_at")
    return _parse_time(timestamp) if isinstance(timestamp, str) else None


def _review_timestamp(review: dict[str, Any]) -> datetime:
    timestamp = (
        review.get("submitted_at")
        or review.get("created_at")
        or "1970-01-01T00:00:00+00:00"
    )
    return _parse_time(str(timestamp))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _thread_root_id(comment: dict[str, Any]) -> int | None:
    root_id = comment.get("in_reply_to_id") or comment.get("id")
    return root_id if isinstance(root_id, int) else None


def _is_codex_bot_user(user: Any) -> bool:
    return str((user or {}).get("login") or "") in CODEX_BOTS


def _is_bot_user(user: Any) -> bool:
    login = str((user or {}).get("login") or "")
    return (
        _is_codex_bot_user(user)
        or (user or {}).get("type") == "Bot"
        or login.endswith("[bot]")
    )


def _is_codex_reply_body(body: str) -> bool:
    return body.startswith("[codex]")


def _is_codex_review_body(body: str) -> bool:
    return body.startswith("## Codex Review")
