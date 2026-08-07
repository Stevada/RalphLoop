"""Turning a finished session into an Outcome.

Three classifiers, one per unattended actor, because each is asked a different question and each is
structurally incapable of the others' answers.

`impasse` can only come from an Implementer — an Editor session cannot fail to deliver a spec it was
never given, and an Integrator is not given one at all.

`INTEGRATION_FAILED` is unreachable from the Implementer's and the Editor's: it does not classify
their sessions at all — the Implementer session already succeeded, and only the merge gate can
raise it against one. It *is* reachable from the Integrator's, because there the merge gate is
asking about a session it dispatched itself.
"""

from __future__ import annotations

from ralph.harness.model.session import Outcome, SessionTelemetry
from ralph.harness.model.verdict import EditorVerdict

WALL_CLOCK_EXIT = 124
"""`timeout(1)`'s exit code. A session that ran past its wall-clock bound is infra-failed."""


def _died_on_the_clock(t: SessionTelemetry) -> bool:
    return t.killed == "wall-clock" or t.exit_code == WALL_CLOCK_EXIT


def classify_implementer(t: SessionTelemetry) -> Outcome:
    """Zero commits is never a benign skip — it is an `impasse`."""
    if _died_on_the_clock(t):
        return Outcome.INFRA_FAILED
    if t.impasse_report is not None or t.commits == 0:
        return Outcome.IMPASSE
    return Outcome.SUCCESS


def classify_integrator(t: SessionTelemetry, merge_finished: bool) -> Outcome:
    """Success is a finished merge, not a commit count.

    `merge_finished` is the harness's own observation of the worktree, and it is the only thing
    asked about the work. A session can resolve a conflict perfectly, leave it staged, and stop —
    at which point the branch carries the Implementer's commits and nothing else, so a commit count
    reads exactly as it would on success. Counting commits here would call that a landing.
    """
    if _died_on_the_clock(t):
        return Outcome.INFRA_FAILED
    if not merge_finished:
        return Outcome.INTEGRATION_FAILED
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
