"""The cycle cap.

A **cycle** is one Implementer session plus the Editor session that follows it. At most three per
sub-issue, hard-enforced: the harness dispatches no fourth Implementer session, whatever the Editor
says. `ceiling-exceeded` and `infra-failed` spend no cycle at all — no Editor is involved in
either, so there is no cycle to spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from ralph.domain.model.graph import SubIssueId


@dataclass(slots=True)
class CycleLedger:
    MAX_CYCLES: ClassVar[int] = 3
    _spent: dict[SubIssueId, int] = field(default_factory=dict)

    def spend(self, id: SubIssueId) -> int:
        """Record a cycle against `id` and return the new count."""
        raise NotImplementedError

    def exhausted(self, id: SubIssueId) -> bool:
        """True once `MAX_CYCLES` cycles have been spent. No further Implementer session runs."""
        raise NotImplementedError

    def must_be_terminal(self, id: SubIssueId) -> bool:
        """True on the final cycle. The Editor may not return `revise`; the harness rejects it
        rather than trusting the Editor to remember the rule."""
        raise NotImplementedError
