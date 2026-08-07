"""The transcripts: everything each session said, one file per session.

The run log stays skimmable by carrying no bodies — it answers *what happened, in what order*. This
is where the bodies go, and the two artifacts index each other without either having to reference
the other: a `session-finished` event already names the sub-issue and the actor, and the cycle is
the only thing a reader brings to the filename.

`<sub-issue>/<cycle>-<actor>.log` is a total key by construction. One Implementer session per cycle,
one Editor session after it, and at most one Integrator dispatched by the merge gate during that
cycle's landing — so no two sessions ever compete for a name.

Nothing in the harness reads these back. They are for the human who opens a quarantined worktree and
wants to know what the session was thinking, which is the one question the notification cannot
answer and the diff can only partly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ralph.harness import Actor
from ralph.issues import SubIssueId


@dataclass(frozen=True, slots=True)
class FileTranscripts:
    """One file per session, under `root`."""

    root: Path

    async def write(self, sub_issue: SubIssueId, cycle: int, actor: Actor, body: str) -> None:
        """Written even when `body` is empty. A session that said nothing is a fact about that
        session, and an absent file would claim instead that no session ran."""
        path = self.root / str(sub_issue) / f"{cycle}-{actor.value}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
