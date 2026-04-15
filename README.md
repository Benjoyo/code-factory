<div align="center">

<h1>Code Factory</h1>

<p><strong>🏭 Orchestrate coding agents via Kanban — high autonomy, isolated per-issue workspaces, single-file repo-owned workflow contract</strong></p>

<p><a href="#quick-start">Quick Start</a> · <a href="https://github.com/Benjoyo/code-factory/blob/main/docs/cli.md">CLI</a> · <a href="https://github.com/Benjoyo/code-factory/blob/main/docs/workflow/README.md">Workflow</a> · <a href="https://github.com/Benjoyo/code-factory/blob/main/SPEC.md">Specification</a></p>

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/Benjoyo/code-factory/main/docs/images/code-factory-dashboard.png" alt="Code Factory operator dashboard showing live issue execution, throughput, token usage, and operator links" width="1257" />
</p>

Code Factory is a Python asyncio implementation and extension of the OpenAI Symphony spec. It
polls tracker work, creates isolated per-issue workspaces, runs coding-agent
sessions inside them, and keeps the workflow contract versioned in
`WORKFLOW.md`.

Use it when you want repeatable issue execution, repo-owned workflow policy,
and enough observability to operate concurrent agent runs without building a
custom harness around your coding agent.

## Typical Workflow

![Typical Code Factory workflow showing tracker intake, per-issue workspace creation, coding-agent execution, operator review, and issue state progression](https://raw.githubusercontent.com/Benjoyo/code-factory/main/docs/code-factory-typical-workflow.svg)

## What You Need

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- A valid `WORKFLOW.md`
- Access to the tracker configured in `WORKFLOW.md`
- A working coding-agent command available to `codex.command`

## Installation

Install `cf` from PyPI as a `uv` tool:

```bash
uv tool install code-factory-agent
```

Then run it directly:

```bash
cf --help
cf serve --no-guardrails
```

If you prefer not to install the tool, you can still run it from the repo with
`uv run cf ...`.

## Quick Start

### 1. Install from PyPI

```bash
uv tool install code-factory-agent
```

### 2. Create a starter workflow in a new project

```bash
cf init
```

`cf init` walks you through the starter values, renders a
project-specific `WORKFLOW.md`, and copies this repo's bundled skills into
`./.agents/skills`. Re-run with `--force` if you want to overwrite an existing
workflow or skills bundle.

Most projects should make a few repo-specific edits before first real use. The
starter workflow is intentionally generic; adapt the bootstrap, verification,
and review setup to your stack.

Example additions to copy into `WORKFLOW.md` and tailor:

```yaml
hooks:
  after_create: |
    git clone --depth 1 git@github.com:your-org/your-repo.git .
    uv sync

states:
  "In Progress":
    hooks:
      before_complete: |
        make verify
  "Rework":
    hooks:
      before_complete: |
        make verify

review:
  prepare: |
    uv sync
  servers:
    - name: app
      base_port: 8000
      command: |
        uv run python -m uvicorn your_project.app:app --host 127.0.0.1 --port {{ review.port }}
      url: http://127.0.0.1:{{ review.port }}
```

Use these as patterns, not defaults:

- `hooks.after_create`: install dependencies, build generated assets, or run any one-time workspace bootstrap your repo needs.
- `states.<state>.hooks.before_complete`: run the quality gate your team expects before handoff, for example `make verify`, `uv run pytest -q`, or a lint/test script.
- `review.prepare` and `review.servers`: make `cf review` immediately useful by starting the exact app or dev server a reviewer should inspect.

### 3. Start the service

```bash
cf serve --no-guardrails
```

If you omit the workflow path, the CLI defaults to `./WORKFLOW.md`.
By default, rotating file logs are written to `./log/code-factory.log` beside
the workflow. Override the root for one run with `--logs-root`, or change/disable
it in `observability.file_logging`.

### 4. Create issues and move to Todo

- Create new issues in Linear Backlog
- Move ready-for-dev issues to Todo

### 5. Steer agents during execution (optional)

Run:

```bash
cf steer ENG-123 "also add integration tests please"
```

This appends operator guidance to an in-flight issue turn.

### 6. Review PRs

Run:

```bash
cf review ENG-123
```

This will:

- Launch a review worktree and any configured review servers.
- Open the browser automatically, if configured.
- Let you quickly submit PR comments with any problems you find.

### 7. Move issues to Merging, Todo, or Rework

Move reviewed issues to:

- Merging, if review was successful
- Todo, if you left review comments in the PR
- Rework, if you left review comments and want a full, clean re-attempt at the issue

## CLI Overview

The main operator commands are:

- `cf init` to bootstrap a repo-local workflow and bundled skills
- `cf serve` to run the long-lived automation service
- `cf review` to launch a review worktree and any configured review servers
- `cf steer` to append operator guidance to an in-flight issue turn
- `cf issue`, `cf comment`, `cf workpad`, and `cf tracker` for tracker-facing
  operator actions

See [docs/cli.md](https://github.com/Benjoyo/code-factory/blob/main/docs/cli.md) for the general CLI reference and
[docs/ticket-cli.md](https://github.com/Benjoyo/code-factory/blob/main/docs/ticket-cli.md) for ticket-oriented commands.

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

- [Workflow docs](https://github.com/Benjoyo/code-factory/blob/main/docs/workflow/README.md)
- [Frontmatter reference](https://github.com/Benjoyo/code-factory/blob/main/docs/workflow/frontmatter.md)
- [Prompt template reference](https://github.com/Benjoyo/code-factory/blob/main/docs/workflow/prompt-template.md)
- [Specification](https://github.com/Benjoyo/code-factory/blob/main/SPEC.md)

## Observability

Code Factory exposes a local observability API and, when stderr is attached to a
TTY, a live terminal dashboard for operators. See
[docs/observability.md](https://github.com/Benjoyo/code-factory/blob/main/docs/observability.md) for endpoints, dashboard
behavior, rotating file logs, and steering/discovery details.

## Runtime Notes

- Startup validates the workflow and required dispatch settings before the
  scheduler loop begins.
- `WORKFLOW.md` is hot-reloaded automatically; valid changes affect future
  dispatches without restarting the service.
- Only issues in active workflow states are dispatched.

## Development

For local development from a checkout:

```bash
git clone git@github.com:Benjoyo/code-factory.git
cd code-factory
make setup
```

Run the CLI directly from the repo with `uv run`:

```bash
uv run cf --help
uv run cf serve --no-guardrails
```

If you want the checkout on your PATH during development, install the local
editable tool:

```bash
uv tool install --editable .
```

Run the full verification suite:

```bash
make verify
```

Create and publish a release in one command:

```bash
make release-patch
make release-minor
make release-major
```

Each release target runs `make verify`, bumps `project.version`, creates a
matching annotated `v...` tag, and pushes the current branch plus tag to
`origin`. Override the remote with `make release-patch REMOTE=<remote>` if
needed.
