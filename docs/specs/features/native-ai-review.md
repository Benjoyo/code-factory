# Native AI Review

## Problem Statement

Code Factory currently relies on the implementing agent's own judgment plus deterministic completion gates before a state transition is accepted. That leaves a gap for review-oriented feedback that is independent from the implementing agent's thread context, shaped for bug-finding rather than implementation, and targeted to the exact patch about to transition. Users need a native AI review feature that can be configured in workflow state definitions, triggered only when practical review conditions are met, and fed back into the implementing agent as repair guidance before the tracker transition is applied.

The review feature must remain operationally simple. Users should be able to define a small number of reusable review types in the workflow, attach those review types to one or more states, and let Code Factory decide whether each review should run based on changed paths and changed lines. The feature should use Codex's native review capability and structured review output, but Code Factory must control review request composition so ticket metadata and workflow-defined review instructions are always present.

## Solution

Code Factory will add workflow-configurable AI review types that run as a separate native Codex review step after deterministic completion gates pass and before the issue transition is committed. Each review runs in a fresh review context rather than the implementing agent's thread, so the reviewer evaluates the candidate patch without inheriting the implementation conversation.

Each review type will define:

- A referenced review prompt section from the workflow body
- The review model and reasoning effort
- Trigger rules based on current worktree diff statistics and changed paths

Each state may request zero, one, or multiple review types. When the implementing agent returns a transition result, Code Factory will:

1. Run existing deterministic readiness and `before_complete` gates
2. Compute the current worktree diff for the candidate patch
3. Evaluate configured review triggers for the current state
4. Run all triggered review types through native Codex review in fresh review sessions
5. Filter low-confidence findings using an internal threshold
6. Merge the remaining findings into one repair prompt
7. Feed that combined prompt back to the implementing agent in the existing completion loop
8. Re-run until either the patch passes review or the shared repair-loop budget is exhausted

The review request sent to Codex will be composed by Code Factory. It will include the review scope instructions, the ticket description and relevant metadata, and the user-defined review overlay prompt. Native Codex review remains responsible for the reviewer rubric and structured JSON output format.

## User Stories

1. As a workflow author, I want to define reusable review prompt sections, so that multiple states can share the same review policy without duplicating text.
2. As a workflow author, I want to define review types separately from states, so that review behavior is configured once and referenced declaratively.
3. As a workflow author, I want a state to request multiple review types, so that a change can be checked from more than one perspective when needed.
4. As a workflow author, I want to choose a model and reasoning effort per review type, so that expensive reviews can be reserved for higher-value checks.
5. As a workflow author, I want to trigger review only for meaningful changes, so that tiny changes do not waste time and tokens.
6. As a workflow author, I want path triggers that express frontend-only, backend-only, and mixed-change review policies, so that reviews stay relevant to the patch.
7. As a workflow author, I want path trigger names that are easy to understand, so that workflow configuration is readable without memorizing set-theory semantics.
8. As a workflow author, I want `paths.only`, `paths.include`, and `paths.exclude` semantics, so that I can express practical review routing with minimal confusion.
9. As a workflow author, I want review triggers based on current worktree changes, so that the reviewer evaluates the exact patch about to transition.
10. As a workflow author, I want changed-line thresholds to use added plus deleted lines, so that refactors and deletions are counted realistically.
11. As an implementing agent, I want review findings returned as one combined repair prompt, so that I can address all accepted findings in one follow-up turn.
12. As an implementing agent, I want review feedback to be based on a separate reviewer context, so that the review is less biased by my prior reasoning.
13. As an implementing agent, I want ticket context included in the review request, so that the reviewer can judge the patch against the intended work rather than the diff alone.
14. As an operator, I want AI review to remain inside the normal worker lifecycle, so that issue transitions still happen through one consistent orchestration path.
15. As an operator, I want deterministic gates to run before AI review, so that obviously invalid patches fail cheaply before review tokens are spent.
16. As an operator, I want all triggered review types to run, so that review behavior is explicit and not dependent on declaration order.
17. As an operator, I want low-confidence findings filtered out automatically, so that the implementing agent only receives higher-signal repair feedback.
18. As an operator, I want the AI review loop to share the existing completion feedback budget, so that workers cannot get stuck in unbounded review-repair cycles.
19. As an operator, I want the first version to keep review artifacts runtime-only, so that tracker comments are not polluted by transient AI review chatter.
20. As an operator, I want review results visible through runtime updates and observability, so that I can understand why an issue was sent back for repair.
21. As a future maintainer, I want review triggering, review execution, and feedback synthesis to be separate deep modules, so that they can be tested in isolation and changed independently.
22. As a future maintainer, I want workflow validation to reject invalid review references and malformed trigger definitions early, so that runtime behavior remains predictable.
23. As a future maintainer, I want the review integration to preserve the existing structured transition contract, so that review adds a gate rather than a parallel state machine.
24. As a spec maintainer, I want implementation work to call out any behavior changes that affect runtime policy, so that `SPEC.md` can be updated carefully and intentionally.

## Implementation Decisions

- Add a workflow-level review configuration namespace for reusable AI review types. This is separate from the existing operator review workspace configuration.
- Add review prompt sections to the workflow body using the same named-section pattern already used for agent prompts, with state definitions referencing review prompt identifiers rather than embedding prompt text inline.
- Extend state configuration so agent-run states can declare one or more review type references. Auto states will not support AI review.
- Keep the reviewer isolated from the implementing agent by running native Codex review in a fresh review context rather than on the implementing session's thread history.
- Compose the native review request text inside Code Factory. The rendered request will include:
  - instructions describing the review scope to inspect
  - ticket description and relevant ticket metadata
  - the user-defined review overlay prompt
- Use the current worktree as the default review surface for v1. Review triggers and review execution will both evaluate the exact worktree diff present at the end of the implementing turn.
- Model trigger rules as a small validated contract rather than a general rules engine. The path trigger interface will use:
  - `only`: every changed file must match one of these globs
  - `include`: at least one changed file must match one of these globs
  - `exclude`: no changed file may match any of these globs
- Keep line-based triggers as scalar thresholds alongside path rules. `lines_changed` will mean added lines plus deleted lines from the selected worktree diff.
- When multiple review types trigger for one state transition, run all of them and merge their filtered findings into one repair prompt.
- Run AI review only after deterministic readiness and `before_complete` hooks pass, so review sees a cleaner candidate patch and token spend is reduced on obviously invalid work.
- Treat AI review as another completion gate inside the existing repair loop rather than introducing a separate orchestration phase.
- Reuse the current completion feedback-loop budget for AI review retries instead of adding a separate review retry budget in v1.
- Consume Codex's structured review output format and apply an internal confidence threshold to findings before surfacing them to the implementing agent.
- Do not make confidence thresholds or finding filtering user-configurable in v1.
- Synthesize one combined repair prompt from all accepted review findings and feed that prompt back into the implementing agent's existing turn loop.
- Keep review persistence runtime-only in v1. Review results may be surfaced through worker updates and observability payloads, but they will not be posted to the tracker by default.
- Prefer a small set of new deep modules:
  - a review workflow/config model and validator
  - a diff-trigger evaluator
  - a review prompt and ticket-context renderer
  - a native review runner that wraps Codex review execution and result parsing
  - a review feedback synthesizer for completion-loop reuse
- Preserve the existing transition result contract. The implementing agent continues to emit the same structured state result; AI review only determines whether the worker accepts that result immediately or sends repair feedback first.
- Implementation of this feature changes workflow/runtime policy and will require a careful `SPEC.md` update before or alongside behavior changes.

## Testing Decisions

- Good tests for this feature should assert external behavior: when review runs, which review types run, which findings survive filtering, what repair feedback is sent, and when transitions are blocked or allowed.
- Add focused parser and validation tests for workflow review configuration, including invalid review references, unsupported keys, malformed path trigger definitions, and duplicate normalized names.
- Add isolated trigger-evaluation tests that cover:
  - `only`, `include`, and `exclude` path semantics
  - mixed frontend and backend changes
  - tiny changes filtered out by line thresholds
  - multiple review types triggering on the same worktree
- Add isolated prompt-rendering tests that verify ticket metadata and review overlay prompts are included in the rendered native review request.
- Add isolated review-result tests for:
  - structured output parsing
  - low-confidence finding filtering
  - combined repair-prompt synthesis from multiple review types
- Add worker/completion-loop tests that verify:
  - deterministic gates run before AI review
  - filtered review findings send the implementing agent back for repair
  - clean review results allow the transition to proceed
  - exhausted shared repair-loop budget returns the failure path cleanly
- Add app-server protocol tests for native review invocation only to the extent needed to verify Code Factory's wrapper behavior, not Codex internals already covered upstream.
- Follow the existing repo style of tight unit tests around parsing and orchestration helpers, plus integration tests around workflow lifecycle and worker behavior.
- Maintain full line and branch coverage for any new branches introduced by review triggering, filtering, and repair-loop integration.

## Out of Scope

- User-configurable confidence thresholds or other finding-filtering controls
- Persisting AI review findings to tracker comments by default
- Building a general boolean trigger DSL beyond the shallow path and line-threshold contract
- Supporting AI review for auto-transition states
- Alternative review surfaces such as latest commit or merge-base diff in v1
- Review-specific retry budgets separate from the existing completion feedback-loop budget
- Cross-review deduplication heuristics beyond straightforward merged feedback formatting
- Exposing native review lifecycle details directly to workflow authors beyond the configured review types and triggers
- Replacing or removing existing deterministic completion gates

## Further Notes

- The existing top-level `review` settings are already used for operator-side human review worktrees. AI review should use a distinct workflow configuration surface to avoid overloading that contract.
- Because native Codex review is being used with Code Factory-rendered request text, the implementation should be explicit that this feature uses native review execution and output while Code Factory owns request composition and trigger policy.
- The first version should optimize for understandable workflow authoring and a small runtime surface area, even if later versions add more review targets or persistence options.
- Documentation updates will need to cover workflow front matter, prompt-section conventions for review prompts, and the new completion-loop behavior when review findings are returned to the implementing agent.
