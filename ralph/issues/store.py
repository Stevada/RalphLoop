"""The issue tracker seam.

The scheduler reads the issue graph once, asks for the current brief/findings per sub-issue, and
mirrors run-log events back for humans. Filesystem markdown and Linear both satisfy this interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ralph.issues.content import Brief, Findings
from ralph.issues.graph import IssueGraph, SubIssueId
from ralph.issues.state import SubIssueState
from ralph.runlog import Event


@runtime_checkable
class IssueStore(Protocol):
    """The issue tracker: filesystem markdown or Linear.

    `write_event` mirrors a transition back into the tracker, and `publish_notification` gives the
    tracker the final human-facing run summary. Both are **best-effort** — the tracker is a
    convenience for humans, and a run must not die because it was unreachable. The harness's own
    authoritative record is the `RunLog`, which is a different sink with different durability.
    """

    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]: ...

    def content(self, id: SubIssueId) -> tuple[Brief, Findings]: ...

    async def record_revision(self, id: SubIssueId, brief: Brief, findings: Findings) -> None: ...

    async def write_event(self, e: Event) -> None: ...

    async def publish_notification(self, body: str) -> None: ...
