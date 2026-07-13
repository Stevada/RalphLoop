"""What the Editor returns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ralph.domain.model.content import Brief, Findings


class Verdict(StrEnum):
    REVISE = "revise"
    PLANNING_DEFECT = "planning-defect"
    INCONCLUSIVE = "inconclusive"

    @property
    def is_terminal(self) -> bool:
        return self is not Verdict.REVISE


@dataclass(frozen=True, slots=True)
class EditorVerdict:
    verdict: Verdict
    revised_brief: Brief | None  # required iff REVISE
    revised_findings: Findings | None
    rationale: str
