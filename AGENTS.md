# AGENTS.md

## 1. Overview
This package is a long-running automation service that reads tracker work, creates isolated per-issue workspaces, and runs coding-agent sessions inside them. It exists to keep workflow policy in-repo, make issue execution repeatable, and provide enough observability to operate concurrent agent runs; when behavior is unclear, check `SPEC.md`.

## 2. Folder Structure
- `src/symphony`: Python package entrypoints, shared models, and package roots.
  - `application`: service bootstrap, logging setup, and top-level runtime wiring.
  - `runtime`: orchestrator, worker, runtime messages, and subprocess/process lifecycle code.
  - `trackers`: generic tracker boundary plus concrete tracker implementations.
  - `coding_agents`: generic coding-agent boundary plus concrete coding-agent implementations.
  - `config`: typed settings models, parsing, defaults, and validation helpers.
  - `workflow`: `WORKFLOW.md` loading, front-matter parsing, and workflow snapshot/state handling.
  - `workspace`: workspace path safety, hook execution, and workspace lifecycle management.
  - `observability`: operator-facing API payloads and HTTP server.
  - `prompts`: workflow prompt rendering and continuation prompt generation.
- `tests`: behavior, protocol, integration, and architecture tests; keep new tests close to the layer they protect.
- `pyproject.toml`: package metadata, lint/type/test tool configuration, and `uv` dependency management.
- `README.md`: operator-facing usage and API/CLI documentation.

## 3. Core Behaviors & Patterns
- The orchestrator owns authoritative runtime state, workers report events back through messages, and concrete integrations stay behind generic boundaries.
- Logging is structured around standard library loggers with issue/session context added at call sites; startup, reload, hook, and protocol failures are logged explicitly rather than swallowed.
- Error handling favors validation up front, early returns on ineligible work, and bounded retries for transient failures; workflow reload errors keep the last known good snapshot active.
- Architecture is package-oriented and intentionally strict: concrete integrations live under `trackers/*` and `coding_agents/*`, utilities are package-local, and architecture tests guard import boundaries and file-size limits.

## 4. Conventions
- Put helper functions in utility/support modules, not inside business modules such as actors, managers, services, or clients.
- Keep source files under 300 lines, prefer focused package-local models over global catch-all model files, and add comments where the control flow is not already obvious.

## 5. Working Agreements
- Preserve behavior parity with `SPEC.md`, update it carefully before introducing new policy. Make it transparent to the user when a change would require a SPEC update.
- When changing runtime behavior, follow the existing layer boundaries instead of adding shortcuts across packages or adding root-level implementation modules.
- Add or update tests with behavior changes, including architecture rules when moving package boundaries or concrete/generic seams.
- Tests have 100% line and branch coverage with hard gates, add or adjust tests faithfully to keep coverage 100%. Use pragmas only for platform-specific behavior or branches otherwise unreasonable to test. 
- Run targeted tests (with uv) while iterating, then run full gates (format check, lint, style, coverage, tests) before handoff: `make verify`.
