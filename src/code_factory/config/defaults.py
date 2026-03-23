"""Workflow defaults shared across CLI, service, and tests."""

from __future__ import annotations

import os
import tempfile

DEFAULT_PROMPT_TEMPLATE = """
You are working on a tracked issue.

Identifier: {{ issue.identifier }}
Title: {{ issue.title }}
{% if issue.upstream_tickets != blank %}

Blocked-by tickets:
{% for upstream in issue.upstream_tickets %}
- {{ upstream.identifier }}{% if upstream.title %}: {{ upstream.title }}{% endif %}{% if upstream.id %} [id: {{ upstream.id }}]{% endif %}{% if upstream.state %} ({{ upstream.state }}){% endif %}
{% if upstream.results_by_state != blank %}
{% for state_result in upstream.results_by_state %}
  - {{ state_result[0] }} summary: {{ state_result[1].summary }}
{% endfor %}
{% else %}
  - No persisted state summaries yet.
{% endif %}
{% endfor %}
{% endif %}

Body:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}
""".strip()

DEFAULT_WORKSPACE_ROOT = os.path.join(tempfile.gettempdir(), "code-factory-workspaces")
