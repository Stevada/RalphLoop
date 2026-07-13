"""The impasse report: the model's story, as it tells it.

The Editor's only sensor, and a defendant's statement — so the harness corroborates it rather
than believing it. The harness's own facts live in `SessionTelemetry`; the two are put side by
side in a `FailureReport`, and their *disagreeing* is itself a signal worth surfacing.

This module is a leaf: it is what an Implementer session emits, and it knows nothing about how
the harness classified the session.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Approach:
    """One thing the Implementer tried, and why it abandoned it."""

    tried: str
    abandoned_because: str


@dataclass(frozen=True, slots=True)
class ImpasseReport:
    """Parsed from the `<impasse>` sentinel. Everything here is the model's narration."""

    failing_test: str
    assertion_output: str  # verbatim, not summarised
    approaches: tuple[Approach, ...]
    unsatisfiable_criterion: str  # the acceptance criterion it believes cannot be met
    what_would_satisfy: str  # what would make it satisfiable
