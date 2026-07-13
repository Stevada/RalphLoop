"""The failure report: what the Editor is handed.

The model's story, checked against the harness's facts. `claim` is the Implementer's narration
and may be absent — a `silent-red` session authored no impasse report because it did not believe
it had failed, and an `integration-failed` session authored none because it had already succeeded
when the merge queue rejected it. `telemetry` and `suite` are never absent: they are what the
harness observed, and they are what the claim is checked against.

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
    claim: ImpasseReport | None  # the model's story. absent for silent-red and integration-failed.
    telemetry: SessionTelemetry  # the harness's facts. always present.
    suite: SuiteResult
    integration_detail: str | None = None  # the merge queue's record, when it raised the failure
