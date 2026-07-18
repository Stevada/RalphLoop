"""Turning a finished session into an Outcome.

Two classifiers, because `impasse` can only come from an Implementer — an Editor session cannot
fail to deliver a brief it was never given — and the Editor's classifier is structurally incapable
of returning it.

`INTEGRATION_FAILED` is unreachable from either: it does not classify a session at all. The
Implementer session already succeeded, green in isolation. Only the merge queue can raise it.
"""

from __future__ import annotations

from ralph.harness.model.session import Outcome, SessionTelemetry, SuiteResult
from ralph.harness.model.verdict import EditorVerdict

WALL_CLOCK_EXIT = 124
"""`timeout(1)`'s exit code. A session that ran past its wall-clock bound is infra-failed."""


def _died_on_the_clock(t: SessionTelemetry) -> bool:
    return t.killed == "wall-clock" or t.exit_code == WALL_CLOCK_EXIT


def classify_implementer(t: SessionTelemetry, suite: SuiteResult) -> Outcome:
    """Zero commits is never a benign skip — it is an `impasse`. The suite result, not the exit
    code, is the outcome: a model's exit code is its opinion, the suite is a fact.
    """
    if _died_on_the_clock(t):
        return Outcome.INFRA_FAILED
    if t.impasse_report is not None or t.commits == 0 or not suite.green:
        return Outcome.IMPASSE
    return Outcome.SUCCESS


def classify_editor(t: SessionTelemetry, verdict: EditorVerdict | None) -> Outcome:
    """Editor success is a verdict returned. An Editor that produced none failed, whatever it
    exited with. Cannot yield IMPASSE.
    """
    if _died_on_the_clock(t):
        return Outcome.INFRA_FAILED
    if verdict is None:
        return Outcome.INFRA_FAILED
    return Outcome.SUCCESS
