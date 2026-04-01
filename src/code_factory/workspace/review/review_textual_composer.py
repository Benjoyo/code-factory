"""Ticket-only PR comment composer for the review TUI."""

from __future__ import annotations

from textual import events
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static, TextArea

from .review_comments import SubmittedReviewComment, submit_review_comment
from .review_models import ReviewTarget


class CommentSubmitted(Message):
    def __init__(self, submitted: SubmittedReviewComment) -> None:
        self.submitted = submitted
        super().__init__()


class CommentFailed(Message):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__()


class CommentCountsUpdated(Message):
    def __init__(self, *, bug_count: int, change_count: int) -> None:
        self.bug_count = bug_count
        self.change_count = change_count
        super().__init__()


class CommentSubmitRequested(Message):
    pass


class CommentToggleRequested(Message):
    pass


class ReviewCommentInput(TextArea):
    async def _on_key(self, event: events.Key) -> None:
        if event.key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(CommentToggleRequested())
            return
        if event.key in {"shift+enter", "ctrl+enter", "ctrl+s"}:
            event.stop()
            event.prevent_default()
            self.post_message(CommentSubmitRequested())
            return
        await super()._on_key(event)


class ReviewCommentComposer(Vertical):
    DEFAULT_CSS = """
    ReviewCommentComposer {
        height: auto;
        border-top: solid $panel;
        padding: 1;
        background: $surface;
    }
    ReviewCommentComposer #comment-row {
        height: 4;
        align: left top;
    }
    ReviewCommentComposer #comment-kind,
    ReviewCommentComposer #comment-submit {
        width: 14;
        height: 4;
    }
    ReviewCommentComposer #comment-kind {
        margin-right: 1;
    }
    ReviewCommentComposer #comment-submit {
        margin-left: 1;
    }
    ReviewCommentComposer #comment-input {
        height: 4;
        min-width: 40;
        width: 1fr;
    }
    """

    def __init__(self, *, repo_root: str, target: ReviewTarget) -> None:
        super().__init__(id="comment-panel")
        self._repo_root = repo_root
        self._target = target
        self._comment_kind = "Change"
        self._submitting_comment = False
        self._submitted_bug_count = 0
        self._submitted_change_count = 0

    def compose(self):
        with Horizontal(id="comment-row"):
            yield Button(
                self._kind_label(self._comment_kind),
                id="comment-kind",
                variant="primary",
            )
            yield ReviewCommentInput(
                id="comment-input",
                show_line_numbers=False,
                placeholder="Capture a change request or bug while testing.",
            )
            yield Button("> Submit", id="comment-submit", variant="success")

    def focus_input(self) -> None:
        self.query_one("#comment-input", TextArea).focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "comment-kind":
            self.action_toggle_comment_kind()
        elif button_id == "comment-submit":
            self.action_submit_comment()

    def on_comment_submit_requested(self) -> None:
        self.action_submit_comment()

    def on_comment_toggle_requested(self) -> None:
        self.action_toggle_comment_kind()

    def action_toggle_comment_kind(self) -> None:
        if self._submitting_comment:
            return
        self._comment_kind = "Bug" if self._comment_kind == "Change" else "Change"
        self.query_one("#comment-kind", Button).label = self._kind_label(
            self._comment_kind
        )

    def action_submit_comment(self) -> None:
        if self._submitting_comment:
            return
        text = self.query_one("#comment-input", TextArea).text
        if not text.strip():
            self._set_status("Review comment text can't be blank.")
            return
        self._submitting_comment = True
        self._set_comment_controls(disabled=True)
        self.run_worker(self._submit_comment(text), group="comment", exclusive=True)

    async def _submit_comment(self, text: str) -> None:
        try:
            submitted = await submit_review_comment(
                self._repo_root,
                pr_number=self._target.pr_number or 0,
                pr_url=self._target.pr_url or "",
                kind=self._comment_kind,
                body=text,
            )
        except Exception as exc:
            self.post_message(CommentFailed(str(exc)))
            return
        self.post_message(CommentSubmitted(submitted))

    def on_comment_submitted(self, message: CommentSubmitted) -> None:
        if message.submitted.kind == "Bug":
            self._submitted_bug_count += 1
        else:
            self._submitted_change_count += 1
        self.post_message(
            CommentCountsUpdated(
                bug_count=self._submitted_bug_count,
                change_count=self._submitted_change_count,
            )
        )
        self.query_one("#comment-input", TextArea).clear()
        self.focus_input()
        self._set_status("")
        self._submitting_comment = False
        self._set_comment_controls(disabled=False)

    def on_comment_failed(self, message: CommentFailed) -> None:
        self._set_status(message.message)
        self.focus_input()
        self._submitting_comment = False
        self._set_comment_controls(disabled=False)

    def _set_comment_controls(self, *, disabled: bool) -> None:
        self.query_one("#comment-kind", Button).disabled = disabled
        self.query_one("#comment-submit", Button).disabled = disabled
        self.query_one("#comment-input", TextArea).disabled = disabled

    def _set_status(self, message: str) -> None:
        status = self.screen.query_one("#status", Static)
        status.update(message)
        status.display = bool(message)

    def _kind_label(self, kind: str) -> str:
        return "~ Change" if kind == "Change" else "! Bug"
