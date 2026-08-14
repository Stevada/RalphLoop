"""Copilot as an SDK-backed Implementer, Integrator, and Editor — its actors and the SDK session
they run on.

`cli.py` names this package; nothing else does. The roles no longer share a CLI invocation or a
log parser, so they live as siblings (`actors.py`) over one session module (`session.py`) rather
than tangled in a single file.
"""

from ralph.adapters.copilot.actors import (
    CopilotEditor,
    CopilotImplementer,
    CopilotIntegrator,
    copilot_editor,
    copilot_implementer,
    copilot_integrator,
)

__all__ = [
    "CopilotEditor",
    "CopilotImplementer",
    "CopilotIntegrator",
    "copilot_editor",
    "copilot_implementer",
    "copilot_integrator",
]
