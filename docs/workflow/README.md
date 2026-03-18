# Workflow Docs

This directory documents the two user-facing parts of `WORKFLOW.md`:

- [Prompt templates](/Users/bennet/git/code-factory/docs/workflow/prompt-template.md): how the Markdown body is rendered, which variables exist, and what makes a template robust.
- [Frontmatter](/Users/bennet/git/code-factory/docs/workflow/frontmatter.md): the YAML schema, defaults, validation rules, supported enum values, and implementation notes.

`WORKFLOW.md` always has the same high-level shape:

```md
---
# YAML frontmatter
tracker:
  kind: linear
---
# Markdown prompt template
You are working issue {{ issue.identifier }}.
```

If the file does not start with `---`, the whole file is treated as the prompt template and the config map is empty.
