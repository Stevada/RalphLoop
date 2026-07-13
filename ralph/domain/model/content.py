"""Content: the mutable half of a sub-issue. Structure is frozen; content is state.

Two fields, deliberately. The brief is the spec and stays clean as spec; the findings are a
channel for adding information *without* lowering the bar.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Brief:
    """What "landed" means for this sub-issue. Acceptance criteria in prose, tests as prose.

    This is what a human diffs against the Planner's original intent, and the thing that softens
    under the spec-drift bet.
    """

    body: str
    revision: int = 0  # 0 is the Planner's original and is never overwritten


@dataclass(frozen=True, slots=True)
class Findings:
    """What the last session learned about the repo — "the client's retry logic swallows the
    expected error."

    Difficulty-neutral by intent. Kept out of the brief so the brief stays clean as spec.
    """

    body: str
