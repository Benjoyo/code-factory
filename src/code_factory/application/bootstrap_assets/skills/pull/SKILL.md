---
name: pull
description:
  Sync the current local branch with its remote state when available, then
  merge the remote default base branch and resolve conflicts (aka
  update-branch). Use when Codex needs to update the harness-prepared issue
  branch with origin, perform a merge-based update (not rebase), and guide
  conflict resolution best practices.
---

# Pull

## Workflow

1. Verify git status is clean or commit/stash changes before merging.
2. Ensure rerere is enabled locally:
   - `git config rerere.enabled true`
   - `git config rerere.autoupdate true`
3. Confirm remotes and branches:
   - Ensure the `origin` remote exists.
   - Ensure the current branch is the harness-prepared issue branch for this run.
   - The branch name may come from tracker metadata or a harness-generated
     fallback. Treat the checked-out branch as canonical for the run.
4. Fetch latest refs:
   - `git fetch origin`
5. Sync branch-specific remote state first when available:
   - Prefer the configured upstream:
     - `git rev-parse --abbrev-ref --symbolic-full-name @{upstream}`
     - If it resolves, run `git pull --ff-only`.
   - If no upstream is configured, check for a same-named remote branch:
     - `branch=$(git branch --show-current)`
     - `git show-ref --verify --quiet "refs/remotes/origin/$branch"`
     - If it exists, run `git pull --ff-only origin "$branch"`.
   - If neither an upstream nor a same-named `origin/<branch>` ref exists, skip
     the branch-sync step and note that this is expected for a harness-prepared
     branch without remote tracking.
6. Merge in order:
   - Resolve the remote default base branch:
     - `base_ref=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD || true)`
     - `base_ref=${base_ref:-origin/main}`
   - Prefer `git -c merge.conflictstyle=zdiff3 merge "$base_ref"` for clearer
     conflict context.
7. If conflicts appear, resolve them (see conflict guidance below), then:
   - `git add <files>`
   - `git commit` (or `git merge --continue` if the merge is paused)
8. Verify with project checks (follow repo policy in `AGENTS.md`).
9. Summarize the merge:
   - Identify the checked-out branch and whether it came from existing tracking
     or had no remote-tracking ref.
   - Call out the most challenging conflicts/files and how they were resolved.
   - Note any assumptions or follow-ups.

## Conflict Resolution Guidance (Best Practices)

- Inspect context before editing:
  - Use `git status` to list conflicted files.
  - Use `git diff` or `git diff --merge` to see conflict hunks.
  - Use `git diff :1:path/to/file :2:path/to/file` and
    `git diff :1:path/to/file :3:path/to/file` to compare base vs ours/theirs
    for a file-level view of intent.
  - With `merge.conflictstyle=zdiff3`, conflict markers include:
    - `<<<<<<<` ours, `|||||||` base, `=======` split, `>>>>>>>` theirs.
    - Matching lines near the start/end are trimmed out of the conflict region,
      so focus on the differing core.
  - Summarize the intent of both changes, decide the semantically correct
    outcome, then edit:
    - State what each side is trying to achieve (bug fix, refactor, rename,
      behavior change).
    - Identify the shared goal, if any, and whether one side supersedes the
      other.
    - Decide the final behavior first; only then craft the code to match that
      decision.
    - Prefer preserving invariants, API contracts, and user-visible behavior
      unless the conflict clearly indicates a deliberate change.
  - Open files and understand intent on both sides before choosing a resolution.
- Prefer minimal, intention-preserving edits:
  - Keep behavior consistent with the branch’s purpose.
  - Avoid accidental deletions or silent behavior changes.
- Resolve one file at a time and rerun tests after each logical batch.
- Use `ours/theirs` only when you are certain one side should win entirely.
- For complex conflicts, search for related files or definitions to align with
  the rest of the codebase.
- For generated files, resolve non-generated conflicts first, then regenerate:
  - Prefer resolving source files and handwritten logic before touching
    generated artifacts.
  - Run the CLI/tooling command that produced the generated file to recreate it
    cleanly, then stage the regenerated output.
- For import conflicts where intent is unclear, accept both sides first:
  - Keep all candidate imports temporarily, finish the merge, then run lint/type
    checks to remove unused or incorrect imports safely.
- After resolving, ensure no conflict markers remain:
  - `git diff --check`
- When unsure, note assumptions and ask for confirmation before finalizing the
  merge.

## When To Ask The User (Keep To A Minimum)

Do not ask for input unless there is no safe, reversible alternative. Prefer
making a best-effort decision, documenting the rationale, and proceeding.

Ask the user only when:

- The correct resolution depends on product intent or behavior not inferable
  from code, tests, or nearby documentation.
- The conflict crosses a user-visible contract, API surface, or migration where
  choosing incorrectly could break external consumers.
- A conflict requires selecting between two mutually exclusive designs with
  equivalent technical merit and no clear local signal.
- The merge introduces data loss, schema changes, or irreversible side effects
  without an obvious safe default.
- The checked-out branch is clearly not the intended issue branch, or the
  remote/default-base information cannot be determined locally.

Otherwise, proceed with the merge, explain the decision briefly in notes, and
leave a clear, reviewable commit history.
