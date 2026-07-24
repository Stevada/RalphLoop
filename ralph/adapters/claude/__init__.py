"""Claude Code as an Editor package.

`cli.py` names this package; nothing downstream does. Claude Code only fills the Editor role today,
but it still has the same package shape as the other vendor adapters: `actors.py` owns the port
implementation, and `session.py` owns the SDK boundary.
"""

from ralph.adapters.claude.actors import ClaudeCodeEditor, claude_editor
from ralph.adapters.claude.session import MODEL, claude_sdk_session

__all__ = [
    "MODEL",
    "ClaudeCodeEditor",
    "claude_editor",
    "claude_sdk_session",
]
