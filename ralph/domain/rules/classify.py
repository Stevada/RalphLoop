"""Turning a finished session into an Outcome.

Two classifiers, because `impasse` and `silent-red` can only come from an Implementer — an Editor
session cannot declare itself stuck — and the Editor's classifier is structurally incapable of
returning them.

`INTEGRATION_FAILED` is unreachable from either: it does not classify a session at all. The
Implementer session already succeeded, green in isolation. Only the merge queue can raise it.
"""

from __future__ import annotations

from ralph.domain.model.session import Outcome, SessionTelemetry, SuiteResult
from ralph.domain.model.verdict import EditorVerdict


def classify_implementer(t: SessionTelemetry, suite: SuiteResult) -> Outcome:
    """Precedence is load-bearing: a ceiling kill and a crash both exit non-zero and are
    indistinguishable downstream unless they are separated here.

    Zero commits is never a benign skip — it is `silent-red`. The suite result, not the exit
    code, is the outcome: a model's exit code is its opinion, the suite is a fact.
    """
    raise NotImplementedError


def classify_editor(t: SessionTelemetry, verdict: EditorVerdict | None) -> Outcome:
    """Editor success is a verdict returned. An Editor that produced none failed, whatever it
    exited with. Cannot yield IMPASSE or SILENT_RED.
    """
    raise NotImplementedError
