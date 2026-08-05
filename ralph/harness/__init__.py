"""The harness decision core: pure, stdlib-only, frozen.

**This module is the interface.** Import from `ralph.harness` — never from
`ralph.harness.rules.routing`. The layout below is an implementation detail, and callers should not
have to learn it.

Inside, two halves, and the dependency arrow between them points one way only:

- `model/` — the **nouns**. Frozen values with no logic: what the system is made of.
- `rules/` — the **verbs**. Pure functions over those values: what the system *decides*. The
  failure taxonomy, the routing table, eligibility, the cycle cap. This is the harness's judgment,
  and everything else in the repo exists to feed it.

`rules/` may import `model/`. `model/` may **not** import `rules/` — a test enforces it.
"""

from ralph.harness.model.consumption import NOTHING, TokenConsumption, total
from ralph.harness.model.failure import FailureReport
from ralph.harness.model.impasse import Approach, ImpasseReport
from ralph.harness.model.preflight import Check, Refusal, RepoFacts
from ralph.harness.model.session import Actor, Killed, Outcome, SessionTelemetry, SuiteResult
from ralph.harness.model.verdict import EditorVerdict, Verdict
from ralph.harness.rules.classify import classify_editor, classify_implementer
from ralph.harness.rules.cycles import CycleLedger
from ralph.harness.rules.eligibility import build_order, eligible, never_eligible
from ralph.harness.rules.preflight import refusals
from ralph.harness.rules.report import failure_report
from ralph.harness.rules.routing import Destination, route

__all__ = [
    # model — the nouns
    "Actor",
    "Approach",
    "Check",
    "EditorVerdict",
    "FailureReport",
    "ImpasseReport",
    "Killed",
    "NOTHING",
    "Outcome",
    "Refusal",
    "RepoFacts",
    "SessionTelemetry",
    "SuiteResult",
    "TokenConsumption",
    "Verdict",
    # rules — the verbs
    "CycleLedger",
    "Destination",
    "build_order",
    "classify_editor",
    "classify_implementer",
    "eligible",
    "failure_report",
    "never_eligible",
    "refusals",
    "route",
    "total",
]
