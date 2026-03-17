from __future__ import annotations

import os
import tempfile

DEFAULT_PROMPT_TEMPLATE = """
You are working on a tracked issue.

Identifier: {{ issue.identifier }}
Title: {{ issue.title }}

Body:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}
""".strip()

DEFAULT_WORKSPACE_ROOT = os.path.join(tempfile.gettempdir(), "symphony_workspaces")
