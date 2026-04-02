# Workflow Frontmatter

This document describes the YAML frontmatter currently accepted by Code Factory's
`WORKFLOW.md` loader, config parser, and workflow-profile validators.

## File format

`WORKFLOW.md` may begin with YAML frontmatter:

```md
---
failure_state: Human Review
tracker:
  kind: linear
states:
  "Todo":
    prompt: default
---
# prompt: default
Your prompt section goes here.
```

Parsing rules:

- Frontmatter is parsed only when the file starts with a line that is exactly `---`.
- Frontmatter ends at the next line that is exactly `---`.
- If the closing fence is missing, the rest of the file is treated as frontmatter and the Markdown body is empty.
- The YAML root must be an object/map.
- Empty frontmatter is allowed and becomes `{}`.
- If frontmatter is absent, the whole file becomes the raw prompt body and the config map is empty.
- Unknown top-level keys are ignored unless another parser reads them.
- A runnable workflow must define top-level `states`.
- A dispatchable workflow must also define top-level `failure_state`.

## Body section parsing

When top-level `states` is present, Code Factory parses the Markdown body into
named sections instead of treating it as one monolithic prompt.

Section rules:

- Only level-1 headings of the form `# prompt: <id>` and `# review: <id>` are recognized.
- `prompt` and `review` in those headings are lowercase and matched literally.
- Section ids are trimmed.
- Non-empty body content outside a named section is invalid.
- At least one `# prompt:` section must exist when `states` is present.
- Prompt section references are matched exactly after trimming.
- Review-section references from `ai_review.types.<name>.prompt` are matched exactly after trimming.

## Top-level keys

Code Factory currently reads these top-level keys:

- `failure_state`
- `terminal_states`
- `tracker`
- `states`
- `ai_review`
- `polling`
- `workspace`
- `agent`
- `codex`
- `hooks`
- `review`
- `observability`
- `server`

## `failure_state`

Global failure fallback used when an agent run blocks or exhausts retries.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `failure_state` | non-blank string | none | Required. Per-state `states.<state>.failure_state` may override it. |

## `terminal_states`

Tracker states treated as terminal by the orchestrator.

| Field | Type | Default |
| --- | --- | --- |
| `terminal_states` | list of strings | `["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]` |

Notes:

- Items are stored as provided.
- The parser validates only that this is a list of strings.

## `tracker`

Tracker configuration controls how issues are discovered and updated.

### Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `tracker.kind` | string or `null` | `null` | Required later by dispatch validation. |
| `tracker.endpoint` | string | `https://api.linear.app/graphql` | Used by the Linear tracker. |
| `tracker.api_key` | string or `$VAR_NAME` or `null` | `LINEAR_API_KEY` env fallback | Empty string resolves to `null`. |
| `tracker.project` | string or `null` | `null` | Required for `linear` dispatch validation. |
| `tracker.assignee` | string or `$VAR_NAME` or `null` | `LINEAR_ASSIGNEE` env fallback | Optional assignee filter. |

### `tracker.kind` values

| Value | Description |
| --- | --- |
| `linear` | Production tracker integration. |
| `memory` | In-memory tracker used by tests and local debugging. |

### `tracker.assignee` behavior

| Value | Description |
| --- | --- |
| `me` | Special value interpreted by the Linear integration as the authenticated viewer. |
| any other non-empty string | Match that assignee id directly. |
| `null`, `""`, or omitted | Do not filter candidate issues by assignee. |

### Secret and env indirection

For `tracker.api_key` and `tracker.assignee`:

- A literal string is used as-is.
- A full `$VAR_NAME` token loads from the environment.
- If the environment variable is unset, the configured fallback is used.
- If the resolved value is `""`, the setting becomes `null`.

## `states`

`states` defines the active tracker states and the per-state workflow behavior.
The active state list is derived directly from the `states` keys.

### Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `states` | object | none | Required. Keys are active tracker states. |
| `states.<state>.prompt` | string or non-empty list of strings | none | Required for agent-run states. References `# prompt: <id>` sections. |
| `states.<state>.ai_review` | string, non-empty list of strings, or object | none | Optional reusable AI review types for agent-run states. |
| `states.<state>.codex.model` | string or `null` | inherit top-level `codex.model` | Agent-run states only. |
| `states.<state>.codex.reasoning_effort` | string or `null` | inherit top-level `codex.reasoning_effort` | Agent-run states only. |
| `states.<state>.codex.fast_mode` | boolean or `null` | inherit top-level `codex.fast_mode` | Agent-run states only. |
| `states.<state>.codex.skills` | list of strings or `null` | inherit current repo-skill allowlist | Agent-run states only. Restricts enabled repo skills to this allowlist. |
| `states.<state>.completion.require_pushed_head` | boolean | `false` | Native completion gate. |
| `states.<state>.completion.require_pr` | boolean | `false` | Native completion gate. Also implies `require_pushed_head`. |
| `states.<state>.hooks.before_complete` | string or `null` | `null` | Optional shell quality gate for agent-run states. |
| `states.<state>.hooks.before_complete_max_feedback_loops` | non-negative integer | `10` | Maximum repair loops for `before_complete` and related native completion feedback. |
| `states.<state>.allowed_next_states` | list of strings | unrestricted | Optional allowlist for structured result `next_state`. |
| `states.<state>.failure_state` | string or `null` | inherit top-level `failure_state` | Optional per-state blocked/failure override. |
| `states.<state>.auto_next_state` | string or `null` | `null` | Makes the state harness-run instead of agent-run. |

### `states.<state>.ai_review` forms

Short form:

```yaml
states:
  "In Progress":
    prompt: default
    ai_review:
      - security
      - frontend
```

Expanded form:

```yaml
states:
  "In Progress":
    prompt: default
    ai_review:
      types:
        - security
        - frontend
      scope: branch
```

Expanded-form fields:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `states.<state>.ai_review.types` | string or non-empty list of strings | none | Required in object form. References `ai_review.types` by name. |
| `states.<state>.ai_review.scope` | `auto`, `worktree`, or `branch` | `auto` | Controls whether review runs against the worktree or branch diff. |

`scope: auto` resolves to:

- `worktree` when native completion is disabled for the state
- `branch` when `completion.require_pushed_head` or `completion.require_pr` is enabled

### Rules

- `states` must be an object with at least one entry.
- State names are trimmed and matched case-insensitively at runtime.
- Duplicate normalized state names are invalid.
- Each state must define exactly one mode:
  - agent-run via `prompt`
  - harness-run via `auto_next_state`
- `prompt` and `auto_next_state` are mutually exclusive.
- Agent-run states require at least one valid prompt reference.
- Auto states must not define `prompt`.
- `allowed_next_states` must be a list of non-blank state names with no duplicate normalized values.
- `failure_state` and `auto_next_state` must be non-blank strings when present.
- `states.<state>.failure_state` must not normalize to the current state name.
- `states.<state>.codex` only supports `model`, `reasoning_effort`, `fast_mode`, and `skills`.
- `states.<state>.completion` only supports `require_pushed_head` and `require_pr`.
- `states.<state>.hooks` only supports `before_complete` and `before_complete_max_feedback_loops`.
- `states.<state>.ai_review` is not supported for auto states.
- `states.<state>.completion` is not supported for auto states.
- `states.<state>.codex` overrides are not supported for auto states.
- `states.<state>.hooks.before_complete` is not supported for auto states.
- `states.<state>.hooks.before_complete_max_feedback_loops` requires `before_complete` unless native completion is enabled for that state.
- Other per-state keys are rejected.

### Runtime behavior

- Agent-run states start a fresh coding-agent session.
- The effective state prompt is the referenced `# prompt:` bodies joined with a blank line, in listed order.
- Auto states do not start an agent. The harness moves the issue directly to `auto_next_state`.
- `allowed_next_states` constrains the structured-result `next_state` enum when configured.
- Per-state `failure_state` overrides the global `failure_state` for that state.
- State `completion` and `hooks.before_complete` participate in the completion/repair loop before the transition is accepted.

Minimal example:

```yaml
states:
  "Todo":
    auto_next_state: In Progress
  "In Progress":
    prompt: default
    ai_review:
      types:
        - security
      scope: auto
    completion:
      require_pr: true
    hooks:
      before_complete: uv run pytest -q
      before_complete_max_feedback_loops: 2
    allowed_next_states:
      - Human Review
      - Blocked
    failure_state: Blocked
  "Merging":
    prompt: merge
    codex:
      model: gpt-5.4-mini
      reasoning_effort: low
      fast_mode: true
      skills:
        - land
```

## `ai_review`

`ai_review` defines reusable workflow-facing AI review types. This is separate
from the top-level `review` section used for operator review worktrees.

### Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `ai_review.types` | object | `{}` | Optional mapping of reusable review-type definitions. |
| `ai_review.types.<name>.prompt` | string | none | Required. References a named `# review: <id>` section. |
| `ai_review.types.<name>.codex.model` | string or `null` | inherit effective session model | Optional review override. |
| `ai_review.types.<name>.codex.reasoning_effort` | string or `null` | inherit effective session reasoning | Optional review override. |
| `ai_review.types.<name>.codex.fast_mode` | boolean or `null` | inherit effective session fast mode | Optional review override. |
| `ai_review.types.<name>.lines_changed` | non-negative integer or `null` | `null` | Optional changed-line threshold trigger. |
| `ai_review.types.<name>.paths.only` | non-empty list of strings | `[]` | Require every changed path to match. |
| `ai_review.types.<name>.paths.include` | non-empty list of strings | `[]` | Require at least one changed path to match. |
| `ai_review.types.<name>.paths.exclude` | non-empty list of strings | `[]` | Require no changed path to match. |

Rules:

- `ai_review` only supports the `types` key.
- `ai_review.types` keys must not be blank.
- Review type names are trimmed and matched case-insensitively.
- Duplicate normalized review type names are invalid.
- `ai_review.types.<name>.prompt` must reference an existing `# review:` section.
- `ai_review.types.<name>.codex` only supports `model`, `reasoning_effort`, and `fast_mode`.
- `ai_review.types.<name>.paths` only supports `only`, `include`, and `exclude`.
- Path-glob lists must contain non-blank strings, must not be empty when present, and must not contain duplicates.

Minimal example:

```yaml
ai_review:
  types:
    security:
      prompt: security
      codex:
        model: gpt-5.4-mini
        reasoning_effort: high
      lines_changed: 25
      paths:
        include:
          - src/**
```

## `polling`

Polling controls how often the workflow store checks for work and workflow changes.

| Field | Type | Default |
| --- | --- | --- |
| `polling.interval_ms` | positive integer or string integer | `30000` |

## `workspace`

Workspace settings control where per-issue workspaces live.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `workspace.root` | path string or `$VAR_NAME` | `<system-temp>/code-factory-workspaces` | Resolved with `expanduser()` and normalized to an absolute path. |

`workspace.root` details:

- `~` is expanded.
- A full `$VAR_NAME` token may point at the real path.
- If that env var is unset or empty, the default workspace root is used.
- Relative paths are accepted and converted to absolute paths.
- Embedded shell interpolation such as `$HOME/workspaces` is not expanded unless the whole value is a single `$VAR_NAME` token.

## `agent`

Agent settings control orchestration concurrency and retry behavior.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `agent.max_concurrent_agents` | positive integer or string integer | `10` | Global concurrent worker limit. |
| `agent.max_retry_backoff_ms` | positive integer or string integer | `300000` | Upper bound for retry backoff. |
| `agent.max_worker_retries` | positive integer or string integer | `3` | Maximum retry attempts after the initial run fails. |
| `agent.max_concurrent_agents_by_state` | object of `state_name -> positive integer` | `{}` | Per-state concurrency overrides. |

`agent.max_concurrent_agents_by_state` details:

- Keys must be non-blank.
- Keys are normalized case-insensitively before lookup.
- Values must be positive integers.

## `codex`

`codex` controls how Code Factory launches and talks to Codex app-server.

Code Factory currently validates only field shape and a few local invariants. It
does not fully enum-validate Codex schema values itself.

### Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `codex.command` | non-empty string | `codex app-server` | Base command launched in the workspace with a shell. |
| `codex.model` | non-blank string or `null` | `null` | Injected as `--model <value>` immediately before `app-server`. |
| `codex.reasoning_effort` | non-blank string or `null` | `null` | Injected as `--config model_reasoning_effort=<value>` immediately before `app-server`. |
| `codex.fast_mode` | boolean or `null` | `null` | When `true`, Code Factory sends fast service-tier metadata on thread start. |
| `codex.approval_policy` | string or object | reject-policy object | Passed through to Codex thread start. |
| `codex.thread_sandbox` | string | `workspace-write` | Passed through to Codex thread start. |
| `codex.turn_sandbox_policy` | object or `null` | `null` | Passed through to Codex turn start. When omitted, Code Factory builds a workspace-scoped default turn policy. |
| `codex.turn_timeout_ms` | positive integer | `3600000` | Per-turn timeout. |
| `codex.read_timeout_ms` | positive integer | `5000` | App-server protocol read timeout. |
| `codex.stall_timeout_ms` | non-negative integer | `300000` | `0` disables stall detection. |

Notes:

- Top-level `codex` does not support a `skills` key.
- If `codex.model`, `codex.reasoning_effort`, or a per-state `codex.skills` allowlist is used, `codex.command` must be valid shell-style argv and must include an explicit `app-server` argument.
- `codex.fast_mode` is protocol-level only. It does not inject CLI flags into `codex.command`.

### `codex.approval_policy`

Accepted shapes:

- string
- object

Common string values currently used with Codex:

| Value | Description |
| --- | --- |
| `untrusted` | Conservative approval mode. |
| `on-failure` | Retry with approval if sandbox execution fails. |
| `on-request` | Ask for escalation explicitly when needed. |
| `never` | Non-interactive mode. Never ask for approval. |

Object form example:

```yaml
codex:
  approval_policy:
    reject:
      sandbox_approval: true
      rules: true
      mcp_elicitations: true
      request_permissions: false
```

Built-in default:

```yaml
codex:
  approval_policy:
    reject:
      sandbox_approval: true
      rules: true
      mcp_elicitations: true
```

### `codex.thread_sandbox`

Common values currently used with Codex:

| Value | Description |
| --- | --- |
| `read-only` | Filesystem is read-only. |
| `workspace-write` | Read access everywhere, write access limited to the workspace and configured writable roots. |
| `danger-full-access` | No filesystem sandboxing. |

### `codex.turn_sandbox_policy`

If `codex.turn_sandbox_policy` is omitted, Code Factory sends a workspace-write
policy rooted at the current workspace path.

Supported policy shapes are passed through as objects. Common variants used with
Codex include:

- `type: dangerFullAccess`
- `type: readOnly`
- `type: externalSandbox`
- `type: workspaceWrite`

## `hooks`

Top-level hooks are shell snippets executed in the workspace directory.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `hooks.after_create` | string or `null` | `null` | Runs only when the workspace directory is newly created. Failure aborts setup. |
| `hooks.before_run` | string or `null` | `null` | Runs before each worker attempt. Failure aborts the attempt. |
| `hooks.after_run` | string or `null` | `null` | Runs after each worker attempt. Failures are logged and ignored. |
| `hooks.before_remove` | string or `null` | `null` | Runs before deleting a workspace directory. Failures are logged and ignored. |
| `hooks.timeout_ms` | positive integer | `60000` | Shared timeout for all top-level hooks. |

## `review`

`review` configures operator review worktrees and optional dev servers used by
the `cf review` flow.

### Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `review.temp_root` | path string, `$VAR_NAME`, or `null` | `null` | Optional root for temporary review worktrees. Resolved to an absolute path. Empty resolves to `null`. |
| `review.prepare` | string or `null` | `null` | Optional command run before review servers start. |
| `review.servers` | non-empty list or omitted | omitted | Optional review server definitions. |
| `review.servers[].name` | non-blank string | none | Must be unique within `review.servers`. |
| `review.servers[].command` | non-blank string | none | Rendered with Liquid using the `review` context and the current server definition as `server`. |
| `review.servers[].base_port` | positive integer or `null` | `null` | For ticket targets, the runtime computes `base_port + trailing_ticket_number`. |
| `review.servers[].url` | string or `null` | `null` | Optional Liquid-rendered browser URL. The same `review` and `server` context is available. |
| `review.servers[].open_browser` | boolean or `null` | `null` | If omitted, opens automatically when `url` is present. |

Notes:

- `review.servers` must be a non-empty list when present.
- `review.temp_root` supports `~` expansion and full `$VAR_NAME` tokens.
- `review.temp_root` differs from `workspace.root`: an unset or empty env token resolves to `null`, not to a default path.

## `observability`

Implementation-specific settings for the live dashboard.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `observability.dashboard_enabled` | boolean | `true` | Only takes effect when stderr is a TTY. |
| `observability.refresh_ms` | positive integer | `1000` | The dashboard currently clamps this to the range `250..1000` ms. |
| `observability.render_interval_ms` | positive integer | `16` | Parsed and stored; currently not used by the live dashboard refresh loop. |

## `server`

Implementation-specific settings for the optional observability HTTP API.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `server.port` | non-negative integer or `null` | `4000` | `0` asks the OS for an ephemeral port. `null` behaves like omission and resolves to the default. CLI `--port` overrides this value for the current run. |
| `server.host` | string | `127.0.0.1` | Bind host for the HTTP server. |

Operational notes:

- The current runtime default is to expose the HTTP API on port `4000`.
- The service starts the HTTP server only at boot.
- Workflow reloads can change the parsed `server.host` and `server.port`, but the running server is not hot-rebound.

## Example frontmatter

```yaml
---
failure_state: Human Review
terminal_states:
  - Closed
  - Cancelled
  - Canceled
  - Duplicate
  - Done

tracker:
  kind: linear
  endpoint: https://api.linear.app/graphql
  api_key: $LINEAR_API_KEY
  project: code-factory
  assignee: me

states:
  "Todo":
    auto_next_state: In Progress
  "In Progress":
    prompt: default
    completion:
      require_pr: true
    hooks:
      before_complete: uv run pytest -q
    ai_review:
      types:
        - security
      scope: auto
    allowed_next_states:
      - Human Review
    failure_state: Blocked
  "Merging":
    prompt: merge
    codex:
      model: gpt-5.4-mini
      reasoning_effort: low
      fast_mode: true
      skills:
        - land

ai_review:
  types:
    security:
      prompt: security
      paths:
        include:
          - src/**

polling:
  interval_ms: 30000

workspace:
  root: ~/code-factory-workspaces

agent:
  max_concurrent_agents: 10
  max_retry_backoff_ms: 300000
  max_worker_retries: 3
  max_concurrent_agents_by_state:
    in progress: 5

codex:
  command: codex app-server
  approval_policy: never
  thread_sandbox: workspace-write
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000

hooks:
  before_run: uv sync --all-extras
  after_run: uv run pytest -q
  timeout_ms: 60000

review:
  temp_root: /tmp/code-factory-review
  prepare: pnpm install
  servers:
    - name: web
      command: pnpm dev --port {{ review.port }}
      base_port: 3000
      url: http://127.0.0.1:{{ review.port }}

observability:
  dashboard_enabled: true
  refresh_ms: 1000
  render_interval_ms: 16

server:
  host: 127.0.0.1
  port: 4000
---
```

## Validation summary

At startup and before dispatching work, Code Factory validates the minimum
config needed to do real work:

- `states` must be present and valid.
- `failure_state` must be present and non-blank.
- `tracker.kind` must be present and supported.
- `tracker.api_key` must be present when required by the selected tracker.
- `tracker.project` must be present when required by the selected tracker.
- `codex.command` must be present and non-empty.

If workflow parsing or validation fails during reload, new dispatches are
blocked until the workflow is fixed. The last known good snapshot stays active
for the running service.
