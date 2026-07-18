"""Building the failure report — and, more importantly, refusing to build part of it.

**The harness never fabricates an impasse report.** A session killed by the clock authored nothing;
an undeclared impasse authored nothing because the model did not believe it had failed; an
`integration-failed` session authored nothing because it had already succeeded when the merge queue
rejected it. Synthesising a plausible-sounding narrative from a partial transcript would be the
least honest artifact this system could produce — a story with no author, handed to the Editor as
though a model had stood behind it.

So there is exactly one place `claim` can come from: `telemetry.impasse_report`, which is `None`
unless the model really emitted the sentinel. This is a four-line function because that is the
entire rule, and because a rule with a name and a test does not quietly acquire an `else` branch.
"""

from __future__ import annotations

from ralph.harness.model.failure import FailureReport
from ralph.harness.model.session import Outcome, SessionTelemetry, SuiteResult


def failure_report(
    outcome: Outcome,
    telemetry: SessionTelemetry,
    suite: SuiteResult,
    integration_detail: str | None = None,
    cycles: int = 1,
) -> FailureReport:
    """The model's story, checked against the harness's facts.

    `telemetry` and `suite` are the facts: commits, diffstat, wall-clock, token consumption, and
    the suite's own output. They are collected independently of the model's narration, so an
    Implementer that claims "all tests pass" alongside a red suite produces a report carrying
    **both** — and the two disagreeing is itself a signal worth surfacing.
    """
    return FailureReport(
        outcome=outcome,
        claim=telemetry.impasse_report,  # the model's, or nothing. never the harness's invention.
        telemetry=telemetry,
        suite=suite,
        integration_detail=integration_detail,
        cycles=cycles,
    )
