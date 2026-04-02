# CLI Reference

Code Factory exposes a single `cf` command. Install it from a local checkout
with `uv tool install --editable .`, or run it from the repo with `uv run cf`.

## Top-Level Commands

### `cf serve`

Runs the long-lived automation service for a workflow.

- `WORKFLOW` defaults to `./WORKFLOW.md` when omitted.
- `--no-guardrails` is required.
- `--logs-root <path>` enables a rotating log file at
  `<path>/log/code-factory.log`.
- `--port <port>` overrides the workflow's configured observability port for the
  current run. `0` is allowed when you want the OS to choose an ephemeral port.
- Bare invocations such as `cf --no-guardrails` are normalized to
  `cf serve --no-guardrails`.

Example:

```bash
cf serve --no-guardrails ./WORKFLOW.md
```

### `cf init`

Bootstraps a project with a starter `WORKFLOW.md` and bundled skills in
`./.agents/skills`.

- Prompts for tracker kind, Linear project name, git repo, states, workspace root, and
  max concurrent agents.
- Writes a workflow using the current `states`-based format and shared prompt
  sections.
- Refuses to overwrite existing bootstrap output unless `--force` is passed.

Example:

```bash
cf init
```

### `cf review`

Creates an operator review worktree for a ticket or for `main`, then starts the
review environment declared in `review:` inside `WORKFLOW.md`.

- `TARGET` is a ticket identifier such as `ENG-123`, or the reserved value
  `main`.
- `--workflow <path>` selects the workflow file to load review configuration
  from.
- `--keep` preserves the generated review worktree after the command exits.
- In an interactive terminal, review launches a Textual TUI with the review
  table, per-server logs, and `review.prepare` output. Outside a TTY it falls
  back to plain console output.

Example:

```bash
cf review ENG-123
```

### `cf steer`

Appends operator guidance to the active turn for a running issue.

- `ISSUE_IDENTIFIER` is the human ticket id, for example `ENG-123`.
- `MESSAGE` is the text added to the active turn.
- `--workflow <path>` selects the workflow whose runtime metadata should be used
  for discovery.
- `--port <port>` bypasses discovery and targets a specific control-plane port.

When `--port` is omitted, Code Factory first checks runtime metadata for the
selected workflow, then falls back to `127.0.0.1:4000`.

Example:

```bash
cf steer ENG-123 "Focus on the failing tests first."
```

## Ticket Commands

Code Factory also includes tracker-facing operator commands:

- `cf issue`
- `cf comment`
- `cf workpad`
- `cf tracker`

See [ticket-cli.md](ticket-cli.md) for the full ticket command reference and
examples.

## Help

Use generated help for the exact current interface:

```bash
cf --help
cf serve --help
cf init --help
cf review --help
cf steer --help
```
