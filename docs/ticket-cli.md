# Ticket CLI

`cf issue`, `cf comment`, and `cf workpad` expose the same Linear-backed ticket
surface used by the agent tools. The CLI is tracker-neutral in naming, but this
repository currently targets Linear.

All commands default to the workflow loaded from `./WORKFLOW.md` unless a
different path is provided through the existing workflow flag.

## Commands

```bash
cf issue get ISSUE [--json]
cf issue list [--project PROJECT] [--team TEAM] [--state STATE] [--query QUERY] [--json]
cf issue create --team TEAM --title TITLE [--project PROJECT] [--json]
cf issue update ISSUE [--json]
cf issue move ISSUE --state STATE [--json]
cf issue link-pr ISSUE --url URL [--title TITLE] [--json]
cf comment list ISSUE [--json]
cf comment create ISSUE [--body BODY | --file FILE | -] [--json]
cf comment update COMMENT [--body BODY | --file FILE | -] [--json]
cf workpad get ISSUE [--json]
cf workpad sync ISSUE [--body BODY | --file FILE | -] [--json]
cf tracker raw --query GRAPHQL [--variables JSON] [--json]
```

## Common Workflows

Inspect issue context:

```bash
cf issue get ENG-123 --json
```

List backlog issues:

```bash
cf issue list --project labelforge-studio --state Backlog --json
```

Create a follow-up ticket:

```bash
cf issue create \
  --team Benjoyo \
  --project labelforge-studio \
  --title "Split out follow-up work" \
  --json
```

Move an issue:

```bash
cf issue move ENG-123 --state "In Progress" --json
```

Sync the workpad from a file:

```bash
cf workpad sync ENG-123 --file workpad.md --json
```

Attach a PR:

```bash
cf issue link-pr ENG-123 --url https://github.com/org/repo/pull/123 --json
```

Upload validation media:

Use `cf tracker raw` for the shared file upload helper when you need an
`assetUrl`, then embed that URL in `cf workpad sync` or `cf comment create`.
This keeps the normal operator commands focused on the ticket workflow while
still leaving one admin escape hatch for edge cases and migration gaps.

`--json` prints the normalized operation result for automation and shell
composition.

## Admin Escape Hatch

`cf tracker raw` is hidden from normal operator guidance and should be used only
for admin work, debugging, or migration gaps. It routes through the same shared
Linear implementation as the higher-level commands.
