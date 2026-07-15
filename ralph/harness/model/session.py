"""How a session ended, as the harness sees it — not as the model reports it."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from ralph.harness.model.impasse import ImpasseReport


class Actor(StrEnum):
    IMPLEMENTER = "implementer"
    EDITOR = "editor"


type Killed = Literal["ceiling", "wall-clock"]
"""Why the harness stopped a session, when it was the harness that stopped it.

The two bounds catch different failures and neither substitutes for the other. `ceiling` means the
session left the smart zone — it was still working, and its judgment was about to stop being worth
trusting. `wall-clock` means it stopped getting anywhere: a session spinning on a failing suite has
a *flat* context and would never trip a ceiling.

Named here, in the harness core, because `classify_implementer` turns it into an `Outcome` and the
adapter that sets it must be spelling the same two words.
"""


class Outcome(StrEnum):
    SUCCESS = "success"
    IMPASSE = "impasse"
    INTEGRATION_FAILED = "integration-failed"
    CEILING_EXCEEDED = "ceiling-exceeded"
    INFRA_FAILED = "infra-failed"


@dataclass(frozen=True, slots=True)
class SessionTelemetry:
    """The harness's word for everything the model cannot observe about itself."""

    exit_code: int
    killed: Killed | None
    peak_context_tokens: int  # the ceiling is on THIS
    consumed_tokens: int  # telemetry only. nothing is gated on it.
    wall_clock_s: float
    commits: int
    diffstat: str
    session_output: str  # the session's own transcript. NOT the suite's — that is SuiteResult.output
    impasse_report: ImpasseReport | None  # parsed from the <impasse> sentinel, out of the above


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """`green`, never `verified`/`trusted`/`passing`. A suite the harness runs is inside the
    blast radius; only CI on a clean checkout is honest."""

    green: bool
    output: str
    duration_s: float
