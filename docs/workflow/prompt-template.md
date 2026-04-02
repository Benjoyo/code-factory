# Workflow Prompt Templates

For runnable workflows, the Markdown body of `WORKFLOW.md` is a set of named
sections. Agent-run state prompts come from `# prompt:` sections, and reusable
AI review overlays come from `# review:` sections.

Sections are declared with level-1 headings:

```md
# prompt: default
Shared instructions.

# prompt: merge
Merge-only instructions.

# review: security
Focus on auth, permissions, and data-exposure bugs.
```

`states.<state>.prompt` in frontmatter may reference one section or a list of
sections. The effective prompt for that state is the referenced section bodies joined with a
blank line between them, in order.

## How rendering works

- When top-level `states` is present, the body is first parsed into named `# prompt: <id>` and `# review: <id>` sections.
- In stateful workflows, every non-empty line in the body must belong to a named section.
- Duplicate prompt section ids are invalid.
- Duplicate review section ids are invalid.
- The effective prompt for the current state is composed before rendering.
- The composed prompt is rendered with a strict [Liquid](https://liquidtemplater.com/)-compatible engine (`python-liquid` with `StrictUndefined`).
- Unknown variables fail rendering.
- Unknown filters fail rendering.
- The composed prompt is trimmed before use.
- If the composed prompt is blank after trimming, Code Factory falls back to its built-in default prompt.
- Each agent-run state dispatch renders its own prompt and starts a fresh coding-agent session.

That means a good template should fully set up the task for the current state without assuming prior thread history.

## Available variables

Only these top-level variables are available:

| Variable | Type | Description |
| --- | --- | --- |
| `issue` | object | The normalized issue snapshot passed to the worker. |
| `attempt` | integer or `nil` | `nil` on the first dispatch for the current state, `1` on the first retry after a failed run, then incrementing for later retries. |

There are no other top-level template variables.

## `issue` object reference

`issue` contains every field from [`Issue`](../../src/code_factory/issues.py).

| Field | Type | Description |
| --- | --- | --- |
| `issue.id` | string or `nil` | Tracker-specific issue id. |
| `issue.identifier` | string or `nil` | Human-facing issue key, for example `ENG-123`. |
| `issue.title` | string or `nil` | Issue title. |
| `issue.description` | string or `nil` | Issue body/description. |
| `issue.priority` | integer or `nil` | Tracker priority as an integer if available. |
| `issue.state` | string or `nil` | Current tracker workflow state. |
| `issue.branch_name` | string or `nil` | Tracker-provided branch name, if any. |
| `issue.url` | string or `nil` | Canonical issue URL. |
| `issue.assignee_id` | string or `nil` | Tracker assignee id, if present. |
| `issue.blocked_by` | array | Array of blocker objects. Empty if there are no blockers. |
| `issue.labels` | array | Array of label names. Empty if there are no labels. |
| `issue.assigned_to_worker` | boolean | Whether the issue is considered assigned to the worker route. |
| `issue.created_at` | string or `nil` | Issue creation time serialized as ISO 8601. UTC datetimes end with `Z`. |
| `issue.updated_at` | string or `nil` | Issue update time serialized as ISO 8601. UTC datetimes end with `Z`. |
| `issue.upstream_tickets` | array | Immediate blocker tickets enriched with parsed state-result summaries. Empty if there are no blockers with ids. |

### `issue.blocked_by[]` object reference

Each blocker entry comes from [`BlockerRef`](../../src/code_factory/issues.py).

| Field | Type | Description |
| --- | --- | --- |
| `blocker.id` | string or `nil` | Tracker-specific blocker id. |
| `blocker.identifier` | string or `nil` | Human-facing blocker key. |
| `blocker.state` | string or `nil` | Current blocker state. |

Example:

```liquid
{% if issue.blocked_by != blank %}
Blocking issues:
{% for blocker in issue.blocked_by %}
- {{ blocker.identifier }}{% if blocker.state %} ({{ blocker.state }}){% endif %}
{% endfor %}
{% endif %}
```

### `issue.upstream_tickets[]` object reference

Each upstream ticket is fetched from the immediate `issue.blocked_by` ids and
enriched with any persisted Code Factory result comments found on that ticket.

| Field | Type | Description |
| --- | --- | --- |
| `upstream.id` | string or `nil` | Tracker-specific issue id. |
| `upstream.identifier` | string or `nil` | Human-facing issue key. |
| `upstream.title` | string or `nil` | Upstream issue title. |
| `upstream.state` | string or `nil` | Current upstream issue state. |
| `upstream.url` | string or `nil` | Canonical upstream issue URL. |
| `upstream.results_by_state` | object | Parsed structured results keyed by the human state name. Empty if none were found. |

Each `upstream.results_by_state["<State Name>"]` entry has:

| Field | Type | Description |
| --- | --- | --- |
| `decision` | string | `transition` or `blocked`. |
| `next_state` | string or `nil` | The state requested by the agent result, if present. |
| `summary` | string | The persisted summary for that state result. |

## Value conversion rules

Before rendering, Code Factory normalizes values for Liquid:

- dataclasses become plain objects
- tuples become arrays
- dictionary keys become strings
- `datetime` values become ISO 8601 strings
- `date` values become `YYYY-MM-DD`
- `time` values become `HH:MM:SS` style ISO strings

## Writing a correct template

Use Liquid syntax, not Jinja or f-string syntax.

Good patterns:

- Guard optional fields with `{% if ... %}`.
- Use `{% else %}` fallbacks for fields that are often missing, especially `issue.description`, `issue.branch_name`, and `attempt`.
- Iterate arrays with `{% for ... %}`.
- Assume strict rendering: if you mistype a variable name, the attempt fails.

Common mistakes:

- Referencing variables that do not exist, such as `ticket`, `issue.body`, or `workflow`.
- Using an unknown filter or custom helper that the Liquid engine does not provide.
- Assuming `attempt` is always an integer.
- Leaving stray non-empty body content outside named sections in a stateful workflow.
- Writing a blank effective prompt and expecting repository-specific instructions to appear automatically.

## Writing a good template

A good workflow prompt usually does four things:

1. Identifies the issue clearly.
2. States the expected outcome in repository-specific terms.
3. Tells the agent how to verify completion.
4. Tells the agent what to do when information is missing or the issue is blocked.

Practical guidance:

- Prefer direct instructions over long meta-prompting.
- Keep repository policy in the prompt, not in the issue body.
- Treat the issue description as input data, not as the only source of truth.
- Be explicit about required tests, linting, review notes, or structured state results.
- If your workflow relies on labels, blockers, priorities, or branch names, reference them conditionally so missing data does not crash the render.
- If you use `attempt`, treat it as retry metadata for a fresh session rather than as a signal that prior thread history is available.

## Example template

```liquid
# prompt: default
You are the coding agent for this repository.

Work the tracked issue below from the current workspace.

Issue:
- Identifier: {{ issue.identifier }}
- Title: {{ issue.title }}
- State: {{ issue.state }}
{% if issue.priority != nil %}- Priority: {{ issue.priority }}{% endif %}
{% if issue.url %}- URL: {{ issue.url }}{% endif %}
{% if issue.branch_name %}- Suggested branch: {{ issue.branch_name }}{% endif %}

{% if issue.labels != blank %}
Labels:
{% for label in issue.labels %}
- {{ label }}
{% endfor %}
{% endif %}

{% if issue.blocked_by != blank %}
Blocking issues:
{% for blocker in issue.blocked_by %}
- {{ blocker.identifier }}{% if blocker.state %} ({{ blocker.state }}){% endif %}
{% endfor %}
{% endif %}

Description:
{% if issue.description %}
{{ issue.description }}
{% else %}
No issue description was provided. Inspect the repository and tracker context before making changes.
{% endif %}

{% if attempt %}
This is retry attempt {{ attempt }}. Rebuild context from the repository and tracker state before continuing.
{% endif %}

Expectations:
- Make the smallest correct change that resolves the issue.
- Run the relevant verification for any changed behavior.
- Leave the repository in a reviewable state.
- If blocked, explain the blocker precisely and stop after collecting the evidence needed to unblock.
```

Example with composition:

```md
---
states:
  "Todo":
    prompt:
      - base
      - execute
  "Merging":
    prompt:
      - base
      - merge
---

# prompt: base
You are working issue {{ issue.identifier }}.

# prompt: execute
Implement and validate the requested change.

# prompt: merge
Land the attached PR and move the issue to Done.

# review: security
Look for security regressions in the candidate patch.
```

## Built-in fallback prompt

If the effective composed prompt is empty, Code Factory uses the default prompt from
[`src/code_factory/config/defaults.py`](../../src/code_factory/config/defaults.py).

That fallback is intentionally minimal. For real workflows, define explicit prompt sections in `WORKFLOW.md`.
