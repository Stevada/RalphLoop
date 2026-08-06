"""Codex as an SDK-backed Implementer, Integrator, and Editor.

`cli.py` names this package; nothing downstream does. The three roles live in `actors.py` over one
JSONL session boundary in `session.py`, matching the shape of the Copilot adapter.
"""

from ralph.adapters.codex.actors import (
    CodexEditor,
    CodexImplementer,
    CodexIntegrator,
    codex_editor,
    codex_implementer,
    codex_integrator,
)
from ralph.adapters.codex.session import (
    EDITOR_SANDBOX,
    IMPLEMENTER_SANDBOX,
    CodexJsonSession,
    CodexUsageError,
    codex_argv,
    codex_read_only_session,
    codex_sdk_session,
    end_of_turn_consumption,
    thread_id,
)

__all__ = [
    "EDITOR_SANDBOX",
    "IMPLEMENTER_SANDBOX",
    "CodexEditor",
    "CodexImplementer",
    "CodexIntegrator",
    "CodexJsonSession",
    "CodexUsageError",
    "codex_argv",
    "codex_editor",
    "codex_implementer",
    "codex_integrator",
    "codex_read_only_session",
    "codex_sdk_session",
    "end_of_turn_consumption",
    "thread_id",
]
