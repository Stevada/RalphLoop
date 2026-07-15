"""The failure report: what the Editor is handed.

The model's story, checked against the harness's facts. `claim` is the Implementer's narration
and may be absent — an undeclared impasse and an `integration-failed` session both authored none.
`telemetry` and `suite` are never absent: they are what the harness observed, and they are what
the claim is checked against.

Lives apart from `impasse.py` because it composes the harness's classification with the model's
narration, and `impasse.py` must stay a leaf that `session.py` can import.
"""

from __future__ import annotations

from dataclasses import dataclass

from ralph.domain.model.impasse import ImpasseReport
from ralph.domain.model.session import Outcome, SessionTelemetry, SuiteResult


@dataclass(frozen=True, slots=True)
class FailureReport:
    outcome: Outcome
    claim: ImpasseReport | None  # the model's story. absent for undeclared impasse, integration-failed.
    telemetry: SessionTelemetry  # the harness's facts. always present.
    suite: SuiteResult
    integration_detail: str | None = None  # the merge queue's record, when it raised the failure
    cycles: int = 1
    """Which cycle produced this — 1 on the first Implementer session, at most `MAX_CYCLES`.

    A harness fact, and the Editor's most valuable piece of context after the diff itself: *"you
    have already rewritten this brief twice and it still fails the same way"* is a much stronger
    reason to return `planning-defect` than anything in the transcript. `must_be_terminal` tells the
    Editor it is out of road; this tells it how much road it has already used.

    It is also what stops a third-cycle escalation from looking, in the notification, exactly like a
    first-cycle one.
    """
