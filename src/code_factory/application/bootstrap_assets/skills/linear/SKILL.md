---
name: linear
description: |
  Tracker operations for CodeFactory agent runs. Use the flat `tracker_*`
  tools for ticket work. Do not use raw tracker access in agent flows.
---

# Tracker Operations

All ticket operations go through the shared tracker tools exposed by
CodeFactory's app server. They handle auth automatically and keep the agent
surface self-explanatory.

Use one operation per tool call. A top-level `errors` array means the operation
failed even if the tool call completed.

## Read

Use these read tools for context gathering:

- `tracker_issue_get` to fetch one issue. Omit `issue` to read the current ticket.
- `tracker_issue_search` to search lightweight issue summaries in the current
  workflow project.

Prefer the narrowest read that answers the question. Ask for comments,
attachments, or relations only when they matter to the task.

## Workpad

CodeFactory hydrates a local `workpad.md` file in the workspace before the run.
Treat that file as the working copy for plan, acceptance criteria, validation
notes, and final handoff summary.

- Edit `workpad.md` locally throughout the run.
- CodeFactory syncs the local workpad back to the tracker automatically during
  the run with a debounce of about 10 seconds.
- CodeFactory also syncs the local workpad again before it persists the final
  state/result transition.

## Write

Use these write tools for explicit mutations:

- `tracker_issue_create` for follow-up tickets and new work in the current
  workflow project.
- `tracker_issue_update` for description, labels, priority, assignee, or
  blockers. Omit `issue` to update the current ticket.
- `tracker_comment_create` and `tracker_comment_update` for non-workpad comments
  when needed.
- `tracker_pr_link` to attach the branch PR to the issue. Omit `issue` to use
  the current ticket.
- `tracker_file_upload` to upload validation media from the workspace.

## Common Workflows

- Inspect issue context with `tracker_issue_get` before making assumptions.
- Keep `workpad.md` current locally and let CodeFactory handle the tracker syncs.
- Create follow-up tickets in the same project when scope spillover is real.
- Attach PRs and validation media as part of the handoff, not as separate
  tracking chores.
- Read other tickets explicitly when the current issue depends on them or needs
  comparison context.

## Rules

- Keep reads and writes narrowly scoped to the task at hand.
- Use the hydrated `workpad.md` file as the preferred progress surface for every ticket.
- Prefer PR attachment and file upload helpers over ad hoc comments.
- Do not use raw tracker queries or schema introspection in agent runs.
