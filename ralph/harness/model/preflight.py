"""What the harness looked at before it agreed to run, and what it found.

`RepoFacts` is the world, gathered by somebody else and handed in — the rule that reads it is pure,
which is why the refusals can be tested without a repository to be wrong about.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Check(StrEnum):
    """The six things the harness refuses to start without.

    Named, and not merely counted, because a refusal that cannot say *which* check failed is a
    refusal a human has to reproduce to understand — and the whole point of a pre-flight is that
    they should not have to.

    Each member's name is its value in SCREAMING_SNAKE_CASE — the canonical slug is the single name
    the check has, in the enum and in `UBIQUITOUS_LANGUAGE.md` both.
    """

    PROTECTED_BRANCH = "protected-branch"
    UNCOMMITTED_CHANGES = "uncommitted-changes"
    NO_TEST_RUNNER = "no-test-runner"
    UNINSTALLED_PRE_COMMIT_HOOKS = "uninstalled-pre-commit-hooks"
    INVALID_ISSUE_SOURCE = "invalid-issue-source"
    INVALID_ISSUE_GRAPH = "invalid-issue-graph"


@dataclass(frozen=True, slots=True)
class Refusal:
    """One reason the run will not start.

    `reason` is prose and it is load-bearing: "your graph has a cycle" and "you are on `main`" are
    different mornings, and a human who is told only that validation failed has been given the
    smaller half of what the harness knows.
    """

    check: Check
    reason: str


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """Everything the pre-flight is allowed to know. Nothing here is looked up; it is all told.

    The four `... _error` fields carry the message from whatever raised, verbatim, rather than a
    re-derived summary of it. The graph's own parser already says exactly what is wrong with the
    graph — restating it here would be a second, worse copy of a message that is already right.
    """

    head_branch: str
    protected: frozenset[str]
    dirty: tuple[str, ...]
    """Paths with uncommitted changes, as git reports them. Empty is clean."""

    suite_error: str | None
    source_error: str | None
    """The message from a source that could not be read at all. `None` means it was read; whether
    what it held was a valid graph is `graph_error`'s question."""

    graph_error: str | None
    pre_commit_config: str | None
    """The repo's pre-commit config, if it has one. `None` means the repo does not use pre-commit,
    which is a fact and not a failure."""

    pre_commit_installed: bool
