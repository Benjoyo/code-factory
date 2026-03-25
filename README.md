# Code Factory (Symphony Python Port)

Asyncio-based Python port of the OpenAI Symphony spec.

This runtime keeps the same user-facing contract as the Elixir reference: it reads `WORKFLOW.md`, polls Linear for eligible issues, runs Codex app-server sessions in per-issue workspaces, hot-reloads workflow changes, and exposes a small observability API when enabled.

Use this port if you want the Symphony behavior and workflow contract in a Python + `uv` environment rather than an Elixir deployment.

## What You Need

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- A valid `WORKFLOW.md`
- Access to the tracker configured in `WORKFLOW.md`
- A working Codex app-server command available to `codex.command`

Create a starter workflow in a new project by running:

```bash
uv run cf init
```

`cf init` now walks you through the starter values with Rich prompts, renders a
project-specific `WORKFLOW.md`, and copies this repo's bundled skills into
`./.agents/skills`. The starter workflow now uses the required `states` mapping
plus a shared `# prompt: default` section, with `Todo` rendered as a harness-run
auto-transition to `In Progress` by default. Re-run with `--force` if you want
to overwrite an existing workflow or skills bundle.

## Running the Service

Run from the package directory with `uv`:

```bash
uv run cf serve --no-guardrails /path/to/WORKFLOW.md
```

Or run it directly with `uvx`:

```bash
uvx --from /Users/bennet/git/code-factory cf serve --no-guardrails /path/to/WORKFLOW.md
```

If you omit the workflow path, the CLI defaults to `./WORKFLOW.md`. Bare service
invocations like `cf --no-guardrails` are routed to `cf serve`.

## Ticket Surfaces

Agent sessions use the shared `tracker_read`, `tracker_write`, and `workpad`
tools for ticket work. Operators use the CLI for the same surface area:

```bash
cf issue get ISSUE
cf issue list [--project PROJECT] [--team TEAM] [--state STATE]
cf issue create --team TEAM --title TITLE
cf issue update ISSUE
cf issue move ISSUE --state STATE
cf issue link-pr ISSUE --url URL
cf comment list ISSUE
cf comment create ISSUE
cf comment update COMMENT
cf workpad get ISSUE
cf workpad sync ISSUE
```

See [docs/ticket-cli.md](docs/ticket-cli.md) for the full command reference and
common workflows. The hidden `cf tracker raw` command is reserved for admin and
debug use only.

## CLI Reference

Top-level commands:

```bash
cf init [--force]
cf review TARGET... [--workflow WORKFLOW] [--keep]
cf serve [OPTIONS] [WORKFLOW]
cf steer ISSUE MESSAGE [--workflow WORKFLOW] [--port PORT]
```

`cf init`

- Prompts for tracker kind, project slug, git repo, state lists, workspace
  root, and max concurrent agents.
- Renders `./WORKFLOW.md` from the bundled meta-template, using the new
  `states` frontmatter mapping and one shared `# prompt: default` body section.
- Copies the packaged skill directories to `./.agents/skills`.
- Refuses to overwrite an existing workflow or skills bundle unless `--force`
  is passed.

`cf serve`

- `--no-guardrails`
  Required acknowledgement flag. The service will print a banner and exit without it.
- `--logs-root <path>`
  Enables a rotating log file at `<path>/log/code-factory.log`.
- `--port <port>`
  Overrides the default local control/observability port. `server.port` in `WORKFLOW.md` is the config-level override and the CLI flag still wins.
  `0` is allowed if you want the OS to choose an ephemeral port.
- `path-to-WORKFLOW.md`
  Explicit workflow file path. If omitted, the CLI uses `WORKFLOW.md` in the current working directory.

`cf review`

- `TARGET...`
  One or more ticket identifiers and/or the reserved keyword `main`.
- `--workflow <path>`
  Workflow path used to load `review:` config and locate the repository root. Defaults to `./WORKFLOW.md`.
- `--keep`
  Keep created review worktrees after the command exits instead of removing them automatically.

`cf review` is an operator-side helper for `Human Review`: it resolves each ticket to the exact open GitHub PR head commit, creates a temporary detached worktree, optionally runs `review.prepare`, and launches the configured dev servers side by side. Server commands can derive stable per-ticket ports from `base_port + ticket_number`.

If a server defines `url`, Code Factory prints it in the summary table. Browser
launch defaults to enabled when `url` is present and can be disabled per server
with `open_browser: false`.

`cf steer`

- `ISSUE`
  Human issue identifier to steer, for example `ENG-123`.
- `MESSAGE`
  Steering text appended to the active in-flight Codex turn.
- `--workflow <path>`
  Workflow path used to discover the running service metadata. Defaults to `./WORKFLOW.md`.
- `--port <port>`
  Override discovery and target a specific local control-plane port directly.

Use `cf --help`, `cf init --help`, `cf serve --help`, and `cf steer --help` for the full generated Typer help output.

## Observability API

The Python port currently ships a JSON API, not a full dashboard UI.

The API starts by default on `127.0.0.1:4000`.

- `server.port` in `WORKFLOW.md` overrides the default port.
- `cf serve --port` overrides both the default and `server.port`.
- If the chosen startup port is already in use, `cf serve` exits and tells you to rerun with a different `--port`.

The service logs the listening URL during startup and writes a small runtime metadata file so `cf steer` can discover custom or ephemeral ports for the current workflow.

Available routes:

- `GET /api/v1/state`
  Returns the current orchestrator snapshot: running workers, retry queue, token totals, and rate-limit data.
- `GET /api/v1/{issue_identifier}`
  Returns the current runtime view for a single issue if that issue is running or queued for retry.
- `POST /api/v1/refresh`
  Triggers an immediate reconcile/poll request and returns whether the request was queued or coalesced.
- `POST /api/v1/{issue_identifier}/steer`
  Appends more user input to the active in-flight Codex turn for that issue. Request body: `{ "message": "..." }`.

Example:

```bash
uv run cf serve \
  --no-guardrails \
  /path/to/WORKFLOW.md
```

Then query:

```bash
curl http://127.0.0.1:4000/api/v1/state
```

Or steer a running issue:

```bash
uv run cf steer ENG-901 "Focus on failing tests first."
```

## What to Expect at Runtime

- Startup validates the workflow and required dispatch settings before the scheduler loop begins.
- `WORKFLOW.md` is hot-reloaded automatically; valid changes affect future dispatches without restarting the service.
- Only issues in active workflow states are dispatched.
- The local control/observability API is always started unless startup fails to bind the selected port.
- Polling Linear is normal service behavior; routine transport-level request logs are suppressed so the service's own logs stay readable.

## Workflow Notes

`WORKFLOW.md` is the main operator surface for this runtime. It controls:

- Tracker configuration
- Active and terminal states
- Workspace root and lifecycle hooks
- Review worktree/dev-server config for operator PR validation
- Codex app-server command, model selection, and sandbox settings
- State-specific prompt sections, transition policies, and harness-run auto transitions
- Optional HTTP server host/port

Operator review environments are configured with a top-level `review:` section:

```yaml
review:
  temp_root: /tmp/code-factory-review
  prepare: pnpm install
  servers:
    - name: web
      base_port: 3000
      command: pnpm dev --port {{ review.port }}
      url: http://127.0.0.1:{{ review.port }}
      open_browser: false
```

The review template context includes `review.target`, `review.kind`, `review.ticket_identifier`, `review.ticket_number`, `review.worktree`, `review.ref`, and `review.port`. Matching `CF_REVIEW_*` environment variables are exported for `prepare` and server commands.

`WORKFLOW.md` uses the top-level `states` mapping as the source of truth for
active workflow states:

- Active states are derived from `states.keys()`.
- A state is agent-run when it defines `prompt`, or harness-run when it defines
  `auto_next_state`.
- Agent-run states may optionally define `allowed_next_states` and
  `failure_state`.
- Agent-run states may optionally define `hooks.before_complete` and
  `hooks.before_complete_max_feedback_loops` to enforce per-state completion
  gates such as tests or lint checks.
- Agent-run states may optionally define `codex.skills` as a repo-local
  allowlist of direct child directories under `.agents/skills`; omitted or
  `null` keeps all repo-local skills available, and `[]` disables all
  repo-local skills for that state.
- When `allowed_next_states` is set, the turn schema constrains `next_state` to
  that set.
- When `failure_state` is set, blocked results always route there regardless of
  any agent-supplied `next_state`.
- `hooks.before_complete` runs after the agent emits a transition result but
  before the harness persists the result or updates the tracker state.
- `before_complete` exit code `0` accepts completion, `2` feeds `stderr` back
  into the same session for another turn up to the configured loop cap, and any
  other non-zero status logs a warning but still allows completion.
- The Markdown body must be split into named `# prompt: <id>` sections for any
  agent-run states.
- Only `codex.model`, `codex.reasoning_effort`, and repo-local `codex.skills`
  can be overridden per agent-run state.
- Agent-run states finish one workflow state per turn using structured output;
  the harness validates the result, persists a state-result comment, and applies
  the tracker transition.
- Auto states do not start an agent session or create a workspace; the harness
  updates the tracker state directly and can dispatch the next active state in
  the same refresh cycle.

If you want parity with the reference behavior, start from the shipped Elixir workflow and adjust only the repo-specific pieces such as `tracker.project_slug`, workspace hooks, and any local paths.

## Development Verification

Install dev dependencies:

```bash
uv sync --all-extras
```

Run the verification suite:

```bash
uv run ruff check .
uv run pyright
uv run python -m pytest -q
```
