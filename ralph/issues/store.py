"""The issue tracker seam.

The scheduler reads the issue graph once, asks for the current brief/findings per sub-issue, and
mirrors run-log events back for humans. Filesystem markdown and Linear both satisfy this interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ralph.issues.content import Brief, Findings
from ralph.issues.consumption import SessionConsumption
from ralph.issues.graph import IssueGraph, SubIssueId
from ralph.issues.state import SubIssueState
from ralph.runlog import Event


@runtime_checkable
class IssueStore(Protocol):
    """The issue tracker: filesystem markdown or Linear.

    `write_event` mirrors a transition back into the tracker, `record_consumption` persists the
    telemetry each actor session consumed, and `publish_notification` gives the tracker the final
    human-facing run summary. The harness's authoritative lifecycle record is the `RunLog`, which
    is a different sink with different durability.
    """

    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]: ...

    def content(self, id: SubIssueId) -> tuple[Brief, Findings]: ...

    def consumption(self, id: SubIssueId) -> tuple[SessionConsumption, ...]: ...

    async def record_revision(self, id: SubIssueId, brief: Brief, findings: Findings) -> None: ...

    async def record_consumption(self, id: SubIssueId, record: SessionConsumption) -> None: ...

    async def write_event(self, e: Event) -> None: ...

    async def publish_notification(self, body: str) -> None: ...
