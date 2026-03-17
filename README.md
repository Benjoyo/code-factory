# Code Factory (Symphony Python Port)

Asyncio-based Python port of the OpenAI Symphony spec.

This runtime keeps the same user-facing contract as the Elixir reference: it reads `WORKFLOW.md`, polls Linear for eligible issues, runs Codex app-server sessions in per-issue workspaces, hot-reloads workflow changes, and exposes a small observability API when enabled.

Use this port if you want the Symphony behavior and workflow contract in a Python + `uv` environment rather than an Elixir deployment.

## What You Need

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/) or `uvx`
- A valid `WORKFLOW.md`
- Access to Linear for the tracker configured in `WORKFLOW.md`
- A working Codex app-server command available to `codex.command`

The easiest starting point for the workflow file is the shipped reference at `../elixir/WORKFLOW.md`.

## Running the Service

Run from the package directory with `uv`:

```bash
uv run symphony --i-understand-that-this-will-be-running-without-the-usual-guardrails /path/to/WORKFLOW.md
```

Or run it directly with `uvx`:

```bash
uvx --from /Users/bennet/git/code-factory symphony --i-understand-that-this-will-be-running-without-the-usual-guardrails /path/to/WORKFLOW.md
```

If you omit the workflow path, the CLI defaults to `./WORKFLOW.md`.

## CLI Reference

Usage:

```bash
symphony [--logs-root <path>] [--port <port>] [path-to-WORKFLOW.md]
```

Flags:

- `--i-understand-that-this-will-be-running-without-the-usual-guardrails`
  Required acknowledgement flag. The service will print a banner and exit without it.
- `--logs-root <path>`
  Enables a rotating log file at `<path>/log/symphony.log`.
- `--port <port>`
  Enables the observability API on that port and overrides `server.port` from `WORKFLOW.md`.
  `0` is allowed if you want the OS to choose an ephemeral port.
- `path-to-WORKFLOW.md`
  Explicit workflow file path. If omitted, the CLI uses `WORKFLOW.md` in the current working directory.

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
uv run symphony \
  --i-understand-that-this-will-be-running-without-the-usual-guardrails \
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
- Codex app-server command and sandbox settings
- Prompt template used for the first turn of each worker attempt
- Optional HTTP server host/port

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
