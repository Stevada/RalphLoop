"""How a session ended, as the harness sees it — not as the model reports it."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from ralph.domain.model.impasse import ImpasseReport


class Actor(StrEnum):
    IMPLEMENTER = "implementer"
    EDITOR = "editor"


class Outcome(StrEnum):
    SUCCESS = "success"
    IMPASSE = "impasse"
    SILENT_RED = "silent-red"
    INTEGRATION_FAILED = "integration-failed"
    CEILING_EXCEEDED = "ceiling-exceeded"
    INFRA_FAILED = "infra-failed"


@dataclass(frozen=True, slots=True)
class SessionTelemetry:
    """The harness's word for everything the model cannot observe about itself."""

    exit_code: int
    killed: Literal["ceiling", "wall-clock"] | None
    peak_context_tokens: int  # the ceiling is on THIS
    consumed_tokens: int  # telemetry only. nothing is gated on it.
    wall_clock_s: float
    commits: int
    diffstat: str
    final_test_output: str
    impasse_report: ImpasseReport | None  # parsed from the <impasse> sentinel


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """`green`, never `verified`/`trusted`/`passing`. A suite the harness runs is inside the
    blast radius; only CI on a clean checkout is honest."""

    green: bool
    output: str
    duration_s: float
