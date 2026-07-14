"""The domain: pure, stdlib-only, frozen.

**This module is the interface.** Import from `ralph.domain` — never from `ralph.domain.model.graph`
or `ralph.domain.rules.routing`. The layout below is an implementation detail, and callers should
not have to learn it.

Inside, two halves, and the dependency arrow between them points one way only:

- `model/` — the **nouns**. Frozen values with no logic: what the system is made of.
- `rules/` — the **verbs**. Pure functions over those values: what the system *decides*. The
  failure taxonomy, the routing table, eligibility, the cycle cap. This is the harness's judgment,
  and everything else in the repo exists to feed it.

`rules/` may import `model/`. `model/` may **not** import `rules/` — a test enforces it.
"""

from ralph.domain.model.content import Brief, Findings
from ralph.domain.model.event import Event, EventKind
from ralph.domain.model.failure import FailureReport
from ralph.domain.model.graph import GraphError, IssueGraph, SubIssue, SubIssueId
from ralph.domain.model.impasse import Approach, ImpasseReport
from ralph.domain.model.notification import Escalation, Notification
from ralph.domain.model.preflight import Check, Refusal, RepoFacts
from ralph.domain.model.session import Actor, Killed, Outcome, SessionTelemetry, SuiteResult
from ralph.domain.model.state import SubIssueState
from ralph.domain.model.verdict import EditorVerdict, Verdict
from ralph.domain.rules.classify import classify_editor, classify_implementer
from ralph.domain.rules.cycles import CycleLedger
from ralph.domain.rules.eligibility import build_order, eligible, never_eligible
from ralph.domain.rules.notify import ATTENTION_ORDER, notify
from ralph.domain.rules.preflight import refusals
from ralph.domain.rules.report import failure_report
from ralph.domain.rules.routing import Destination, route

__all__ = [
    # model — the nouns
    "Actor",
    "Approach",
    "Brief",
    "Check",
    "EditorVerdict",
    "Escalation",
    "Event",
    "EventKind",
    "FailureReport",
    "Findings",
    "GraphError",
    "ImpasseReport",
    "IssueGraph",
    "Killed",
    "Notification",
    "Outcome",
    "Refusal",
    "RepoFacts",
    "SessionTelemetry",
    "SubIssue",
    "SubIssueId",
    "SubIssueState",
    "SuiteResult",
    "Verdict",
    # rules — the verbs
    "ATTENTION_ORDER",
    "CycleLedger",
    "Destination",
    "build_order",
    "classify_editor",
    "classify_implementer",
    "eligible",
    "failure_report",
    "never_eligible",
    "notify",
    "refusals",
    "route",
]
