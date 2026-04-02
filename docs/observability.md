# Observability

Code Factory exposes two operator-facing observability surfaces:

- a local HTTP API
- a live terminal dashboard when stderr is attached to a TTY

## HTTP API

By default the API listens on `127.0.0.1:4000`.

- `server.host` and `server.port` in `WORKFLOW.md` define the configured
  endpoint.
- `cf serve --port` overrides the configured port for the current run.
- `cf serve --port 0` lets the OS choose an ephemeral port.
- The service logs the bound URL during startup and writes runtime metadata so
  other commands can discover the effective endpoint.

Available routes:

- `GET /api/v1/state`
  Returns the current orchestrator snapshot, including active workers, retry
  state, and agent totals.
- `GET /api/v1/{issue_identifier}`
  Returns the current runtime view for one issue when that issue is active or
  queued for retry.
- `POST /api/v1/refresh`
  Requests an immediate reconcile/poll cycle and returns whether the request was
  queued or coalesced.
- `POST /api/v1/{issue_identifier}/steer`
  Appends operator input to the active turn for one issue. Request body:
  `{ "message": "..." }`.

Example:

```bash
curl http://127.0.0.1:4000/api/v1/state
curl -X POST http://127.0.0.1:4000/api/v1/ENG-123/steer \
  -H 'content-type: application/json' \
  -d '{"message":"Focus on failing tests first."}'
```

## Terminal Dashboard

When stderr is a TTY and `observability.dashboard_enabled` is `true`, Code
Factory renders a live Rich dashboard alongside the service logs.

- `observability.refresh_ms` controls the dashboard refresh cadence and is
  clamped to `250..1000` ms.
- `observability.render_interval_ms` is parsed and stored for workflow
  compatibility, but the live dashboard currently refreshes from `refresh_ms`.
- Typing `q`, `quit`, or `exit` in an interactive terminal stops the service.

The dashboard is an operator status surface, not a separate web UI.

## `cf steer` Discovery

`cf steer` discovers the control-plane endpoint in this order:

1. `--port`, if provided
2. runtime metadata written by the running service for the selected workflow
3. the default fallback `127.0.0.1:4000`

That discovery flow lets `cf steer` work with custom ports and ephemeral ports
without forcing you to copy the current bound port manually.
