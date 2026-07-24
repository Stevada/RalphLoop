"""Copilot as an SDK-backed Implementer and Editor — its two actors and the SDK session they run on.

`cli.py` names this package; nothing else does. The two roles no longer share a CLI invocation or a
log parser, so they live as siblings (`actors.py`) over one session module (`session.py`) rather
than tangled in a single file.
"""

from ralph.adapters.copilot.actors import (
    CopilotEditor,
    CopilotImplementer,
    copilot_editor,
    copilot_implementer,
)

__all__ = [
    "CopilotEditor",
    "CopilotImplementer",
    "copilot_editor",
    "copilot_implementer",
]
