"""Content: the mutable half of a sub-issue. Structure is frozen; content is state.

Two fields, deliberately. The spec is the sub-issue's full text and stays a clean bar; the
findings are a channel for adding information *without* lowering it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Spec:
    """What "landed" means for this sub-issue. Its full text — acceptance criteria, tests as
    prose, and everything else the Planner wrote — minus the findings.

    This is what a human diffs against the Planner's original intent, and the thing that softens
    under the spec-drift bet.
    """

    body: str
    revision: int = 0  # 0 is the Planner's original and is never overwritten


@dataclass(frozen=True, slots=True)
class Findings:
    """What the last session learned about the repo — "the client's retry logic swallows the
    expected error."

    Difficulty-neutral by intent. Kept out of the spec so the spec stays a clean bar.
    """

    body: str
