"""The cycle cap.

A **cycle** is one Implementer session plus the Editor session that follows it. At most three per
sub-issue, hard-enforced: the harness dispatches no fourth Implementer session, whatever the Editor
says. `infra-failed` spends no cycle at all — no Editor is involved, so there is no cycle to spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from ralph.issues.graph import SubIssueId


@dataclass(slots=True)
class CycleLedger:
    """A cycle is spent at the moment an Implementer failure routes to the Editor — that is when
    the Editor half begins, and it is the only route that spends one.
    """

    MAX_CYCLES: ClassVar[int] = 3
    _spent: dict[SubIssueId, int] = field(default_factory=dict)

    def spend(self, id: SubIssueId) -> int:
        """Record a cycle against `id` and return the new count."""
        if self.exhausted(id):
            raise ValueError(
                f"{id!r} has already spent {self.MAX_CYCLES} cycles; there is no fourth"
            )
        count = self.spent(id) + 1
        self._spent[id] = count
        return count

    def spent(self, id: SubIssueId) -> int:
        """How many cycles `id` has already spent. Zero before its first session.

        The scheduler reads this to number the attempt it is about to make — `spent(id) + 1` is the
        cycle a session belongs to, and that number ends up in the `FailureReport` the Editor and
        the human both read.
        """
        return self._spent.get(id, 0)

    def exhausted(self, id: SubIssueId) -> bool:
        """True once `MAX_CYCLES` cycles have been spent. No further Implementer session runs."""
        return self.spent(id) >= self.MAX_CYCLES

    def must_be_terminal(self, id: SubIssueId) -> bool:
        """True on the final cycle. The Editor may not return `revise`; the harness rejects it
        rather than trusting the Editor to remember the rule.

        The same fact as `exhausted`, read from the other side of the cycle: once the third cycle
        is under way, a `revise` would ask for a fourth Implementer session, and the scheduler
        would refuse to dispatch one. Two names because two callers ask two different questions —
        one gates the verdict, the other gates the dispatch.
        """
        return self.exhausted(id)
