"""Linear issue values and adapter-local interfaces."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ralph.issues.state import SubIssueState


class LinearIssueStoreError(ValueError):
    """Linear did not contain the parent/sub-issue shape Ralph requires."""


@dataclass(frozen=True, slots=True)
class LinearComment:
    id: str
    body: str


@dataclass(frozen=True, slots=True)
class LinearIssue:
    id: str
    identifier: str
    title: str
    description: str
    state: str
    blocked_by: tuple[str, ...] = ()
    comments: tuple[LinearComment, ...] = ()
    sub_issues: tuple[LinearIssue, ...] = ()


class LinearClient(Protocol):
    def parent_issue(self, identifier: str) -> LinearIssue: ...

    def update_issue_description(self, issue_id: str, description: str) -> None: ...

    def update_issue_state(self, issue_id: str, state: str) -> None: ...

    def create_comment(self, issue_id: str, body: str) -> None: ...


@dataclass(frozen=True, slots=True)
class LinearStateMap:
    """Linear workflow state names for Ralph's four sub-issue states."""

    ready: str = "ready"
    in_progress: str = "in-progress"
    landed: str = "landed"
    needs_human: str = "needs-human"

    def from_linear(self, raw: str) -> SubIssueState:
        normalized = normalize_state(raw)
        for state, name in self._pairs().items():
            if normalized == normalize_state(name):
                return state
        raise LinearIssueStoreError(
            f"Linear state {raw!r} is not a Ralph sub-issue state. "
            f"Expected: {', '.join(self._pairs().values())}."
        )

    def to_linear(self, state: SubIssueState) -> str:
        return self._pairs()[state]

    def _pairs(self) -> Mapping[SubIssueState, str]:
        return {
            SubIssueState.READY: self.ready,
            SubIssueState.IN_PROGRESS: self.in_progress,
            SubIssueState.LANDED: self.landed,
            SubIssueState.NEEDS_HUMAN: self.needs_human,
        }


def normalize_state(raw: str) -> str:
    return re.sub(r"[\s_-]+", "-", raw.strip().casefold())
