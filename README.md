# Code Factory

Code Factory is a Python asyncio implementation of the Symphony service spec: a
long-running automation service that polls tracker work, creates isolated
per-issue workspaces, runs coding-agent sessions inside them, and keeps the
workflow contract versioned in `WORKFLOW.md`.

Use it when you want repeatable issue execution, repo-owned workflow policy,
and enough observability to operate concurrent agent runs without building a
custom harness around your coding agent.

## What You Need

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- A valid `WORKFLOW.md`
- Access to the tracker configured in `WORKFLOW.md`
- A working coding-agent command available to `codex.command`

## Installation

For day-to-day use from a local checkout, install `cf` as a `uv` tool:

```bash
uv tool install --editable .
```

Then run it directly:

```bash
cf --help
cf serve --no-guardrails
```

If you prefer not to install the tool, you can still run it from the repo with
`uv run cf ...`.

## Quick Start

Create a starter workflow in a new project:

```bash
cf init
```

`cf init` walks you through the starter values with Rich prompts, renders a
project-specific `WORKFLOW.md`, and copies this repo's bundled skills into
`./.agents/skills`. Re-run with `--force` if you want to overwrite an existing
workflow or skills bundle.

Start the service:

```bash
cf serve --no-guardrails /path/to/WORKFLOW.md
```

If you omit the workflow path, the CLI defaults to `./WORKFLOW.md`. Bare service
invocations such as `cf --no-guardrails` are routed to `cf serve`.

## CLI Overview

The main operator commands are:

- `cf init` to bootstrap a repo-local workflow and bundled skills
- `cf serve` to run the long-lived automation service
- `cf review` to launch a review worktree and any configured review servers
- `cf steer` to append operator guidance to an in-flight issue turn
- `cf issue`, `cf comment`, `cf workpad`, and `cf tracker` for tracker-facing
  operator actions

See [docs/cli.md](docs/cli.md) for the general CLI reference and
[docs/ticket-cli.md](docs/ticket-cli.md) for ticket-oriented commands.

## Ticket Surfaces

Agent sessions use flat `tracker_issue_*`, `tracker_comment_*`,
`tracker_pr_link`, and `tracker_file_upload` tools for ticket work. The
orchestrator manages `workpad.md` synchronization to a ticket comment
automatically during the run.

Operators can use the CLI for the same ticket surface area:

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

## Workflow

`WORKFLOW.md` is the main operator surface for Code Factory. It keeps tracker
configuration, active states, prompt sections, completion gates, review setup,
workspace hooks, and observability settings in the repo so teams can version and
hot-reload automation policy alongside application code.

See the workflow docs for the current contract:

- [Workflow docs](docs/workflow/README.md)
- [Frontmatter reference](docs/workflow/frontmatter.md)
- [Prompt template reference](docs/workflow/prompt-template.md)
- [Specification](SPEC.md)

## Observability

Code Factory exposes a local observability API and, when stderr is attached to a
TTY, a live terminal dashboard for operators. See
[docs/observability.md](docs/observability.md) for endpoints, dashboard
behavior, and steering/discovery details.

## Runtime Notes

- Startup validates the workflow and required dispatch settings before the
  scheduler loop begins.
- `WORKFLOW.md` is hot-reloaded automatically; valid changes affect future
  dispatches without restarting the service.
- Only issues in active workflow states are dispatched.

## Development

Install dev dependencies:

```bash
make setup
```

Run the full verification suite:

```bash
make verify
```
