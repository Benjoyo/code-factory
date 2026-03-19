# Code Factory (Symphony Python Port)

Asyncio-based Python port of the OpenAI Symphony spec.

This runtime keeps the same user-facing contract as the Elixir reference: it reads `WORKFLOW.md`, polls Linear for eligible issues, runs Codex app-server sessions in per-issue workspaces, hot-reloads workflow changes, and exposes a small observability API when enabled.

Use this port if you want the Symphony behavior and workflow contract in a Python + `uv` environment rather than an Elixir deployment.

## What You Need

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- A valid `WORKFLOW.md`
- Access to Linear for the tracker configured in `WORKFLOW.md`
- A working Codex app-server command available to `codex.command`

Create a starter workflow in a new project by running:

```bash
uv run cf init
```

`cf init` now walks you through the starter values with Rich prompts, renders a
project-specific `WORKFLOW.md`, and copies this repo's bundled skills into
`./.agents/skills`. The starter workflow now uses the optional `states` mapping
plus a shared `# prompt: default` section so it preserves the current monolithic
behavior while making state-specific prompts easy to split later. Re-run with
`--force` if you want to overwrite an existing workflow or skills bundle.

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

## CLI Reference

Top-level commands:

```bash
cf init [--force]
cf serve [OPTIONS] [WORKFLOW]
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
  Enables the observability API on that port and overrides `server.port` from `WORKFLOW.md`.
  `0` is allowed if you want the OS to choose an ephemeral port.
- `path-to-WORKFLOW.md`
  Explicit workflow file path. If omitted, the CLI uses `WORKFLOW.md` in the current working directory.

Use `cf --help`, `cf init --help`, and `cf serve --help` for the full generated Typer help output.

## Observability API

The Python port currently ships a JSON API, not a full dashboard UI.

The API is disabled unless one of these is true:

- `server.port` is set in `WORKFLOW.md`
- `--port` is passed on the CLI

When enabled, the service logs the listening URL during startup.

Available routes:

- `GET /api/v1/state`
  Returns the current orchestrator snapshot: running workers, retry queue, token totals, and rate-limit data.
- `GET /api/v1/{issue_identifier}`
  Returns the current runtime view for a single issue if that issue is running or queued for retry.
- `POST /api/v1/refresh`
  Triggers an immediate reconcile/poll request and returns whether the request was queued or coalesced.

Example:

```bash
uv run cf serve \
  --no-guardrails \
  --port 4000 \
  /path/to/WORKFLOW.md
```

Then query:

```bash
curl http://127.0.0.1:4000/api/v1/state
```

## What to Expect at Runtime

- Startup validates the workflow and required dispatch settings before the scheduler loop begins.
- `WORKFLOW.md` is hot-reloaded automatically; valid changes affect future dispatches without restarting the service.
- Only issues in active workflow states are dispatched.
- If the observability API is not enabled, startup now logs that explicitly.
- Polling Linear is normal service behavior; routine transport-level request logs are suppressed so the service's own logs stay readable.

## Workflow Notes

`WORKFLOW.md` is the main operator surface for this runtime. It controls:

- Tracker configuration
- Active and terminal states
- Workspace root and lifecycle hooks
- Codex app-server command, model selection, and sandbox settings
- State-specific prompt sections used for the first turn of each worker attempt
- Optional HTTP server host/port

`WORKFLOW.md` uses the top-level `states` mapping as the source of truth for
active workflow states:

- Active states are derived from `states.keys()`.
- The Markdown body must be split into named `# prompt: <id>` sections.
- Each state maps to a prompt section id or list of ids.
- Only `codex.model` and `codex.reasoning_effort` can be overridden per state.

When an issue stays active across turns, the worker keeps the same live session
only if the effective state profile is unchanged. If the prompt or allowed
per-state codex settings change, the current worker ends after the completed
turn and the existing continuation retry starts a fresh session in the same
workspace.

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
