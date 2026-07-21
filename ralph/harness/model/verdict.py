"""What the Editor returns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ralph.issues.content import Findings, Spec


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
    revised_spec: Spec | None  # required iff REVISE
    revised_findings: Findings | None  # None means "leave them as they are"
    rationale: str

    def __post_init__(self) -> None:
        """"Required iff REVISE" is an invariant, so it is enforced by the type rather than by a
        comment and a hope.

        Both directions bite. A `revise` with no spec asks the scheduler to restart a sub-issue
        against nothing, and the only place to discover that would be a `None` dereference three
        layers away. A `planning-defect` carrying a spec is stranger still: it is terminal — no
        further Implementer session will ever read that spec — so an Editor that wrote one has
        misunderstood its own verdict, and the harness should say so rather than silently discard
        the work.
        """
        wants_a_spec = self.verdict is Verdict.REVISE
        if wants_a_spec and self.revised_spec is None:
            raise ValueError("a `revise` verdict must carry the spec to restart against")
        if not wants_a_spec and self.revised_spec is not None:
            raise ValueError(
                f"a `{self.verdict.value}` verdict is terminal; nothing will ever read its spec"
            )

    @property
    def revision(self) -> tuple[Spec, Findings | None]:
        """The content the sub-issue restarts against. Only a `revise` verdict has one.

        The `None` findings mean *leave them as they are* — a revision may change the spec without
        touching the findings, or the findings without touching the spec. They are separate fields
        because they are separate ideas: the spec is the bar, and the findings are what the last
        session learned about the repo. Merging the two is how a bar gets lowered by accident.
        """
        if self.revised_spec is None:
            raise ValueError(f"a `{self.verdict.value}` verdict carries no revision")
        return self.revised_spec, self.revised_findings
