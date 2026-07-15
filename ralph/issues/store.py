"""The issue tracker seam.

The scheduler reads the issue graph once, asks for the current brief/findings per sub-issue, and
mirrors run-log events back for humans. Filesystem markdown is the adapter today; Linear can satisfy
the same interface later.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ralph.issues.content import Brief, Findings
from ralph.issues.graph import IssueGraph, SubIssueId
from ralph.issues.state import SubIssueState
from ralph.runlog import Event


@runtime_checkable
class IssueStore(Protocol):
    """The issue tracker: the sub-issue files today, Linear later.

    `write_event` mirrors a transition back into the tracker and is **best-effort** — the tracker
    is a convenience for humans, and a run must not die because it was unreachable. The harness's
    own authoritative record is the `RunLog`, which is a different sink with different durability.
    """

    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]: ...

    def content(self, id: SubIssueId) -> tuple[Brief, Findings]: ...

    async def record_revision(self, id: SubIssueId, brief: Brief, findings: Findings) -> None: ...

    async def write_event(self, e: Event) -> None: ...
