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
    issue_data: dict[str, object] | None = None,
) -> str:
    prompt_template = workflow_prompt(
        workflow_snapshot.prompt_template_for_state(issue.state)
    )
    try:
        template = LIQUID_ENV.from_string(prompt_template)
    except Exception as exc:
        raise RuntimeError(
            f"template_parse_error: {exc} template={prompt_template!r}"
        ) from exc

    try:
        return template.render(
            attempt=attempt,
            issue=to_liquid_value(issue_data if issue_data is not None else issue),
        )
    except Exception as exc:
        raise RuntimeError(f"template_render_error: {exc}") from exc
