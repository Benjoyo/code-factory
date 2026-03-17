from __future__ import annotations

from liquid import Environment, StrictUndefined

from ..config import workflow_prompt
from ..issues import Issue
from ..workflow.models import WorkflowSnapshot
from .values import to_liquid_value

LIQUID_ENV = Environment(undefined=StrictUndefined)


def build_prompt(
    issue: Issue,
    workflow_snapshot: WorkflowSnapshot,
    *,
    attempt: int | None = None,
) -> str:
    prompt_template = workflow_prompt(workflow_snapshot.prompt_template)
    try:
        template = LIQUID_ENV.from_string(prompt_template)
    except Exception as exc:
        raise RuntimeError(
            f"template_parse_error: {exc} template={prompt_template!r}"
        ) from exc

    try:
        return template.render(attempt=attempt, issue=to_liquid_value(issue))
    except Exception as exc:
        raise RuntimeError(f"template_render_error: {exc}") from exc


def continuation_prompt(turn_number: int, max_turns: int) -> str:
    return f"""
Continuation guidance:

- The previous agent turn completed normally, but the tracked issue is still in an active state.
- This is continuation turn #{turn_number} of {max_turns} for the current agent run.
- Resume from the current workspace and workpad state instead of restarting from scratch.
- The original task instructions and prior turn context are already present in this thread, so do not restate them before acting.
- Focus on the remaining ticket work and do not end the turn while the issue stays active unless you are truly blocked.
""".strip()
