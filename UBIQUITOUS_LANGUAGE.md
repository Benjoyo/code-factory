# Ubiquitous Language

This initial draft is derived from the repository guidance in the current conversation and should be refined against `SPEC.md` as terminology hardens.

## Work intake and execution

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Tracker** | An external system that supplies work for the service to evaluate and execute. | Source, provider |
| **Work Item** | A unit of tracked work that may be selected for execution by the service. | Issue, ticket, task |
| **Workflow** | The repo-defined policy and prompt bundle that governs how a Work Item is executed and reviewed. | Runbook, recipe, template |
| **Workflow Snapshot** | The last known good loaded form of a Workflow used for active execution. | Cached workflow, config |
| **Execution** | One end-to-end attempt to complete a single Work Item under a Workflow. | Run, job, attempt |
| **Workspace** | An isolated filesystem environment created for one Execution. | Checkout, working copy |
| **Coding Agent Session** | A coding-agent interaction that runs inside a Workspace as part of an Execution. | Agent run, chat |

## Runtime roles

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Orchestrator** | The runtime authority that owns state and coordinates active Executions. | Scheduler, coordinator |
| **Worker** | A runtime process that performs execution steps and reports results back to the Orchestrator. | Runner, executor |
| **Runtime Message** | A structured event sent from a Worker to the Orchestrator about execution progress or failure. | Callback, signal, log line |
| **Runtime State** | The authoritative current view of active Executions maintained by the Orchestrator. | Cache, status |

## Operations and review

| Term | Definition | Aliases to avoid |
| --- | --- | --- |
| **Operator** | A human responsible for supervising and operating the service. | User, admin |
| **Operator Dashboard** | The operator-facing view of current runtime activity, sessions, and health. | UI, panel |
| **Operator Review** | A human review step performed on execution output in a Workspace. | Manual review |
| **AI Review** | An automated review step performed on execution output in a Workspace. | Bot review, auto review |
| **Hook** | A repo-defined command invoked during the Workspace lifecycle. | Script, callback |
| **Observability** | The signals and interfaces used by Operators to inspect runtime behavior. | Monitoring, telemetry |

## Relationships

- A **Tracker** supplies many **Work Items**.
- An **Execution** handles exactly one **Work Item** under one **Workflow Snapshot**.
- An **Execution** provisions exactly one **Workspace**.
- A **Coding Agent Session** runs inside one **Workspace** and belongs to one **Execution**.
- An **Orchestrator** coordinates many **Workers**.
- A **Worker** sends **Runtime Messages** to the **Orchestrator** while carrying out an **Execution**.
- **Operator Review** and **AI Review** evaluate the output of an **Execution** in its **Workspace**.

## Example dialogue

> **Dev:** "When a **Tracker** reports a new **Issue**, do we create the workspace immediately?"
>
> **Domain expert:** "Call it a **Work Item** unless the tracker's native language matters. We create a **Workspace** when an **Execution** starts."
>
> **Dev:** "Does the **Worker** decide which workflow version to use?"
>
> **Domain expert:** "No. The **Orchestrator** owns that decision and gives the **Worker** a **Workflow Snapshot** for the **Execution**."
>
> **Dev:** "So the **Coding Agent Session** and the **Worker** are not the same thing?"
>
> **Domain expert:** "Correct. The **Worker** is the runtime actor; the **Coding Agent Session** is the agent activity it runs inside the **Workspace**."

## Flagged ambiguities

- "issue", "task", and "tracker work" all refer to the same generic concept; prefer **Work Item** except when a concrete tracker requires its native term.
- "run", "job", and "session" blur three different concepts; prefer **Execution** for the end-to-end attempt, **Worker** for the runtime actor, and **Coding Agent Session** for the agent interaction.
- "workflow" can mean either the authored policy or the loaded active version; prefer **Workflow** for the source definition and **Workflow Snapshot** for the in-memory version used by an Execution.
- "review" is too vague on its own; prefer **Operator Review** or **AI Review** whenever the reviewer type matters.
