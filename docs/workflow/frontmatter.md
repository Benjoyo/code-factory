# Workflow Frontmatter

This document describes the YAML frontmatter accepted by Code Factory's `WORKFLOW.md` loader and config parser.

## File format

The workflow file may begin with YAML frontmatter:

```md
---
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

- Frontmatter is only parsed when the file starts with a line that is exactly `---`.
- Parsing stops at the next line that is exactly `---`.
- The YAML root must be an object/map.
- If frontmatter is absent, the whole file becomes the raw prompt body and the config map is empty.
- Unknown top-level keys are ignored.
- A runnable workflow must define top-level `states`.

## Top-level keys

Core keys used by this runtime:

- `tracker`
- `states`
- `polling`
- `workspace`
- `agent`
- `codex`
- `hooks`
- `observability`
- `server`

Implementation notes:

- `server` is an implementation extension and is already referenced in `SPEC.md`.
- `observability` is implemented in this runtime but is not currently listed in the core `SPEC.md` frontmatter section.

## `tracker`

Tracker configuration controls how issues are discovered and updated.

### Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `tracker.kind` | string | `null` | Required for dispatch validation. |
| `tracker.endpoint` | string | `https://api.linear.app/graphql` | Used by the Linear tracker. |
| `tracker.api_key` | string or `$VAR_NAME` | `LINEAR_API_KEY` env fallback | Empty string is treated as missing. |
| `tracker.project_slug` | string or `null` | `null` | Required for `linear`. |
| `tracker.assignee` | string or `$VAR_NAME` or `null` | `LINEAR_ASSIGNEE` env fallback | Optional routing filter. |
| `tracker.terminal_states` | list of strings | `["Closed", "Cancelled", "Canceled", "Duplicate", "Done"]` | These states are treated as terminal. |

### `tracker.kind` values

| Value | Description |
| --- | --- |
| `linear` | Production tracker integration. Requires `tracker.api_key` and `tracker.project_slug` before dispatch can start. |
| `memory` | In-memory tracker used by tests and local debugging. Accepted by the runtime, but not part of the core `SPEC.md` tracker contract. |

### `tracker.assignee` behavior

`tracker.assignee` is not an enum, but it has one special value:

| Value | Description |
| --- | --- |
| `me` | Resolve the authenticated Linear viewer id and only route issues assigned to that user. |
| any other non-empty string | Match that assignee id directly. |
| `null`, `""`, or omitted | Do not filter candidate issues by assignee. |

### Secret and env indirection

For `tracker.api_key` and `tracker.assignee`:

- a literal string is used as-is
- a full `$VAR_NAME` token loads from the environment
- if the environment variable is unset, the configured fallback is used
- if the resolved value is `""`, the setting becomes `null`

## `states`

`states` defines the active workflow states, selects prompt sections for each
agent-run state, and optionally configures harness-owned transitions.

### Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `states` | object | none | Required. Keys are active tracker states. |
| `states.<state>.prompt` | string or non-empty list of strings | none | Required for agent-run states. References named `# prompt: <id>` sections from the Markdown body. |
| `states.<state>.codex.model` | string or `null` | inherit global `codex.model` | Optional per-state Codex model override for agent-run states only. |
| `states.<state>.codex.reasoning_effort` | string or `null` | inherit global `codex.reasoning_effort` | Optional per-state reasoning override for agent-run states only. |
| `states.<state>.allowed_next_states` | list of strings | unrestricted | Optional allowlist for harness-applied transitions. |
| `states.<state>.failure_state` | string or `null` | `null` | Optional fallback state for `blocked` results with no explicit `next_state`. |
| `states.<state>.auto_next_state` | string or `null` | `null` | Makes the state harness-run instead of agent-run. |

Rules:

- `states` must be an object with at least one entry.
- State names are trimmed and matched case-insensitively at runtime.
- Duplicate normalized state names are invalid.
- Every state must define exactly one mode:
  - agent-run via `prompt`
  - harness-run via `auto_next_state`
- `states.<state>.prompt` may reference one prompt section or several prompt sections.
- Prompt references are matched exactly after trimming.
- Referencing a missing prompt section is invalid.
- Only `codex.model` and `codex.reasoning_effort` may be overridden per state.
- `prompt` and `auto_next_state` are mutually exclusive.
- `codex` overrides are rejected for auto states.
- `allowed_next_states` must be a list of non-blank state names with no duplicate normalized values.
- `failure_state` and `auto_next_state` must be non-blank strings when present.
- `failure_state` must not equal the current state.
- Other per-state keys are rejected.

Runtime behavior:

- Active states are derived from `states` keys.
- Agent-run states start a fresh coding-agent session and render the referenced prompt section bodies concatenated with a blank line between sections, in listed order.
- Auto states do not start an agent. The harness moves the issue directly to `auto_next_state`.
- Successful agent turns return structured output with `decision`, `summary`, and optional `next_state`, and the harness performs the validated state transition.

Minimal example:

```yaml
states:
  "Todo":
    auto_next_state: In Progress
  "In Progress":
    prompt: default
    allowed_next_states:
      - Review
      - Blocked
    failure_state: Blocked
  "Merging":
    prompt: merge
    codex:
      model: gpt-5.4-mini
      reasoning_effort: low
```

## `polling`

Polling controls how often the workflow store checks for work and workflow changes.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `polling.interval_ms` | positive integer or string integer | `30000` | Reloaded dynamically and used for future scheduling. |

## `workspace`

Workspace settings control where per-issue workspaces live.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `workspace.root` | path string or `$VAR_NAME` | `<system-temp>/code-factory-workspaces` | Resolved with `expanduser()` and converted to an absolute path. |

`workspace.root` details:

- `~` is expanded.
- A full `$VAR_NAME` token may point at the real path.
- Relative paths are accepted and then normalized to an absolute path.
- Embedded shell-style interpolation such as `$HOME/workspaces` is not expanded by Code Factory unless the whole value is a single `$VAR_NAME` token.

## `agent`

Agent settings control orchestration concurrency and retry behavior.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `agent.max_concurrent_agents` | positive integer or string integer | `10` | Global concurrent worker limit. |
| `agent.max_retry_backoff_ms` | positive integer or string integer | `300000` | Upper bound for retry backoff. |
| `agent.max_concurrent_agents_by_state` | object of `state_name -> positive integer` | `{}` | Per-state concurrency overrides. |

`agent.max_concurrent_agents_by_state` details:

- Keys must be non-blank.
- Keys are normalized with `strip().lower()` before lookup.
- Values must be positive integers.
- Example: `In Progress: 4` and `in progress: 4` resolve to the same normalized state key.

## `codex`

`codex` config controls how Code Factory launches and talks to Codex app-server.

Code Factory validates only a subset of these fields itself:

- `codex.command` must be a non-empty string
- `codex.model` and `codex.reasoning_effort` must be non-blank strings if present
- `codex.approval_policy` must be a string or object
- `codex.turn_sandbox_policy` must be an object if present

For the enum-like Codex values below, the concrete accepted values were verified against the locally generated app-server JSON schema on March 18, 2026.

### Fields

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `codex.command` | non-empty string | `codex app-server` | Base app-server command launched in the workspace with a shell. |
| `codex.model` | non-blank string or `null` | `null` | Injected as `--model <value>` immediately before `app-server`. |
| `codex.reasoning_effort` | non-blank string or `null` | `null` | Injected as `--config model_reasoning_effort=<value>` immediately before `app-server`. |
| `codex.approval_policy` | string or object | `{"reject":{"sandbox_approval":true,"rules":true,"mcp_elicitations":true}}` | Passed through to Codex thread start. |
| `codex.thread_sandbox` | string | `workspace-write` | Passed through to Codex thread start. |
| `codex.turn_sandbox_policy` | object or `null` | `null` | Passed through to Codex turn start. If omitted, Code Factory builds a workspace-scoped default policy. |
| `codex.turn_timeout_ms` | positive integer | `3600000` | Per-turn timeout. |
| `codex.read_timeout_ms` | positive integer | `5000` | App-server protocol read timeout. |
| `codex.stall_timeout_ms` | non-negative integer | `300000` | `0` disables stall detection. |

`codex.command` remains the base shell command. If `codex.model` or
`codex.reasoning_effort` is set, Code Factory parses the command as shell-style
argv, inserts those flags immediately before the `app-server` argument, and
launches the resulting command. This means the base command should keep
`app-server` as an explicit argument when you use these workflow-managed
overrides.

### `codex.approval_policy` string values

These values come from the Codex `AskForApproval` schema and OpenAI's Codex CLI guidance.

| Value | Description |
| --- | --- |
| `untrusted` | Conservative approval mode. Most commands need approval except a limited safe-read allowlist. |
| `on-failure` | Run in the sandbox first; if the command fails because it needs more access, rerun with approval. |
| `on-request` | Run in the sandbox by default and explicitly request escalation when needed. |
| `never` | Non-interactive mode. Never ask for approval. Work within the available constraints. |

### `codex.approval_policy` object form

The installed schema also accepts a reject-policy object:

```yaml
codex:
  approval_policy:
    reject:
      sandbox_approval: true
      rules: true
      mcp_elicitations: true
      request_permissions: false
```

Object fields:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `reject.sandbox_approval` | boolean | none | Reject sandbox approval requests. |
| `reject.rules` | boolean | none | Reject rules-related approval requests. |
| `reject.mcp_elicitations` | boolean | none | Reject MCP elicitation requests. |
| `reject.request_permissions` | boolean | `false` | Reject permission-request approvals. |

Code Factory's built-in default uses the object form with the first three booleans set to `true`.

### `codex.thread_sandbox` values

These values come from the Codex `SandboxMode` schema.

| Value | Description |
| --- | --- |
| `read-only` | Filesystem is read-only. |
| `workspace-write` | Read access everywhere, write access limited to the workspace and configured writable roots. |
| `danger-full-access` | No filesystem sandboxing. |

### `codex.turn_sandbox_policy`

If `codex.turn_sandbox_policy` is omitted, Code Factory sends this default policy for each turn:

```yaml
codex:
  turn_sandbox_policy:
    type: workspaceWrite
    writableRoots:
      - /absolute/path/to/current/workspace
    readOnlyAccess:
      type: fullAccess
    networkAccess: false
    excludeTmpdirEnvVar: false
    excludeSlashTmp: false
```

Supported policy object variants from the installed Codex schema:

#### `type: dangerFullAccess`

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | enum | none | Must be `dangerFullAccess`. Disables filesystem sandboxing for the turn. |

#### `type: readOnly`

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | enum | none | Must be `readOnly`. |
| `networkAccess` | boolean | `false` | Whether network access is enabled for the turn. |
| `access` | object | `{type: fullAccess}` | Read-only access scope. |

`readOnly.access.type` values:

| Value | Description |
| --- | --- |
| `fullAccess` | Allow reading any path available inside the sandbox. |
| `restricted` | Only allow reads from the listed roots, optionally including platform defaults. |

`readOnly.access` fields when `type: restricted`:

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | enum | none | Must be `restricted`. |
| `includePlatformDefaults` | boolean | `true` | Keep Codex's platform-default readable roots in addition to your explicit roots. |
| `readableRoots` | array of absolute paths | `[]` | Extra readable roots. |

#### `type: externalSandbox`

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | enum | none | Must be `externalSandbox`. |
| `networkAccess` | enum | `restricted` | Network policy for the external sandbox. |

`externalSandbox.networkAccess` values:

| Value | Description |
| --- | --- |
| `restricted` | Use the schema's restricted network mode for the external sandbox. |
| `enabled` | Enable network access in the external sandbox. |

#### `type: workspaceWrite`

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | enum | none | Must be `workspaceWrite`. |
| `writableRoots` | array of absolute paths | `[]` | Additional writable roots for the turn. |
| `readOnlyAccess` | object | `{type: fullAccess}` | Read scope outside writable roots. |
| `networkAccess` | boolean | `false` | Whether network access is enabled for the turn. |
| `excludeTmpdirEnvVar` | boolean | `false` | Exclude the current `TMPDIR`/tmpdir env path from writable roots if Codex would otherwise include it. |
| `excludeSlashTmp` | boolean | `false` | Exclude `/tmp` from writable roots if Codex would otherwise include it. |

`workspaceWrite.readOnlyAccess.type` values are the same as the `readOnly.access.type` values above:

- `fullAccess`
- `restricted`

## `hooks`

Hooks are shell snippets executed in the workspace directory.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `hooks.after_create` | string or `null` | `null` | Runs only when the workspace directory is newly created. Failure aborts setup. |
| `hooks.before_run` | string or `null` | `null` | Runs before each worker attempt. Failure aborts the attempt. |
| `hooks.after_run` | string or `null` | `null` | Runs after each worker attempt. Failures are logged and ignored. |
| `hooks.before_remove` | string or `null` | `null` | Runs before deleting a workspace directory. Failures are logged and ignored. |
| `hooks.timeout_ms` | positive integer | `60000` | Shared timeout for all hooks. |

## `observability`

This is an implementation-specific top-level section for the TUI dashboard.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `observability.dashboard_enabled` | boolean | `true` | Only has an effect when stderr is a TTY. |
| `observability.refresh_ms` | positive integer | `1000` | The live dashboard currently clamps this to the range `250..1000` ms. |
| `observability.render_interval_ms` | positive integer | `16` | Parsed and stored, but currently unused by `LiveStatusDashboard`. |

## `server`

This is an implementation extension for the optional observability HTTP API.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `server.port` | non-negative integer or `null` | `null` | Enables the HTTP API when set. `0` asks the OS for an ephemeral port. CLI `--port` overrides this value. |
| `server.host` | string | `127.0.0.1` | Bind host for the HTTP server. |

Operational notes:

- The service only starts the HTTP server at boot.
- A workflow reload can change `server.port` or `server.host` in the parsed settings, but the current implementation does not hot-rebind an already running server.

## Example frontmatter

```yaml
---
tracker:
  kind: linear
  endpoint: https://api.linear.app/graphql
  api_key: $LINEAR_API_KEY
  project_slug: code-factory
  assignee: me
  terminal_states:
    - Closed
    - Cancelled
    - Canceled
    - Duplicate
    - Done

states:
  "Todo":
    prompt: default
  "In Progress":
    prompt: default
  "Merging":
    prompt: merge
    codex:
      model: gpt-5.4-mini
      reasoning_effort: low

polling:
  interval_ms: 30000

workspace:
  root: ~/code-factory-workspaces

agent:
  max_concurrent_agents: 10
  max_retry_backoff_ms: 300000
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

At startup and before each dispatch tick, Code Factory validates the minimum config needed to do real work:

- `tracker.kind` must be present and supported
- `tracker.api_key` must be present when required by the selected tracker
- `tracker.project_slug` must be present when required by the selected tracker
- `codex.command` must be present and non-empty

If parsing or validation fails, new dispatches are blocked until the workflow is fixed. Existing in-flight work is not forcibly restarted.
