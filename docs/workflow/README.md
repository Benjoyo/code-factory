# Workflow Docs

This directory documents the user-facing parts of `WORKFLOW.md`:

- [Prompt templates](./prompt-template.md): how named prompt sections are declared, composed, and rendered with Liquid.
- [Frontmatter](./frontmatter.md): the YAML schema, defaults, validation rules, supported enum values, and implementation notes.

A runnable `WORKFLOW.md` now has this high-level shape:

```md
---
tracker:
  kind: linear
states:
  "Todo":
    prompt: default
  "In Progress":
    prompt: default
---
# prompt: default
You are working issue {{ issue.identifier }}.
```

Key ideas:

- `states` is required for runnable workflows.
- Active states are derived from `states` keys.
- The Markdown body is split into named `# prompt: <id>` sections.
- `states.<state>.prompt` selects one section or a list of sections for that state.
- Several states can share the same section, so the old monolithic behavior is still expressible by pointing every active state at the same prompt section.
