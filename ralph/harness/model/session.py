"""How a session ended, as the harness sees it — not as the model reports it."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from ralph.harness.model.consumption import TokenConsumption
from ralph.harness.model.impasse import ImpasseReport


class Actor(StrEnum):
    IMPLEMENTER = "implementer"
    EDITOR = "editor"
    INTEGRATOR = "integrator"


type Killed = Literal["wall-clock"]
"""Why the harness stopped a session, when it was the harness that stopped it.

Named here, in the harness core, because `classify_implementer` turns it into an `Outcome` and the
adapter that sets it must spell the same word.
"""


class Outcome(StrEnum):
    SUCCESS = "success"
    IMPASSE = "impasse"
    INTEGRATION_FAILED = "integration-failed"
    INFRA_FAILED = "infra-failed"


@dataclass(frozen=True, slots=True)
class SessionTelemetry:
    """The harness's word for everything the model cannot observe about itself."""

    exit_code: int
    killed: Killed | None
    consumption: TokenConsumption  # telemetry only. nothing is gated on it.
    auto_compactions: int  # telemetry only. nothing is gated on it.
    resumable_identifier: str | None  # telemetry only. nothing is gated on it.
    wall_clock_s: float
    commits: int
    diffstat: str
    session_output: str  # what the model said, as the adapter read it. NOT the suite's output.
    impasse_report: ImpasseReport | None  # parsed from the <impasse> sentinel, out of the above
    transcript: str
    """Everything the session emitted, before the adapter read anything out of it.

    Separate from `session_output` because the two answer to different masters: `session_output` is
    only as complete as the adapter's parser, and a parser is allowed to be behind a vendor's
    schema. What a human is owed cannot be.
    """


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """`green`, never `verified`/`trusted`/`passing`. A suite the harness runs is inside the
    blast radius; only CI on a clean checkout is honest."""

    green: bool
    output: str
    duration_s: float
