"""Builders for domain values.

Not fakes — a fake satisfies a Protocol at a seam; a builder constructs a value. These exist so a
test can state the one field it is actually about and let the rest default to something plausible.
`telemetry(commits=0)` says "a session that committed nothing" and nothing else, which is exactly
what the undeclared-impasse test means.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ralph.domain import (
    Approach,
    EditorVerdict,
    ImpasseReport,
    SessionTelemetry,
    Killed,
    SuiteResult,
    Verdict,
)
from ralph.issues import Brief, Findings, IssueGraph, SubIssue, SubIssueId
from ralph.ports import Observation


def observation(
    context: int = 20_000, *, consumed: int = 50_000, rate_limit: float | None = None
) -> Observation:
    """One model call. `context` is the only number the ceiling reads, so it is the positional one
    — a test that says `observation(130_000)` is saying the thing it means."""
    return Observation(
        context_tokens=context, consumed_tokens=consumed, rate_limit_used_percent=rate_limit
    )


def telemetry(
    *,
    exit_code: int = 0,
    killed: Killed | None = None,
    peak_context_tokens: int = 20_000,
    consumed_tokens: int = 50_000,
    wall_clock_s: float = 60.0,
    commits: int = 1,
    diffstat: str = " 1 file changed, 1 insertion(+)",
    session_output: str = "all green, boss",
    impasse_report: ImpasseReport | None = None,
) -> SessionTelemetry:
    """A green Implementer session, unless you say otherwise."""
    return SessionTelemetry(
        exit_code=exit_code,
        killed=killed,
        peak_context_tokens=peak_context_tokens,
        consumed_tokens=consumed_tokens,
        wall_clock_s=wall_clock_s,
        commits=commits,
        diffstat=diffstat,
        session_output=session_output,
        impasse_report=impasse_report,
    )


def suite(*, green: bool = True, output: str = "1 passed") -> SuiteResult:
    return SuiteResult(green=green, output=output, duration_s=0.0)


def impasse(
    *,
    failing_test: str = "test_it",
    assertion_output: str = "AssertionError: expected 1, got 2",
    approaches: tuple[Approach, ...] = (),
    unsatisfiable_criterion: str = "the third acceptance criterion",
    what_would_satisfy: str = "an API that does not exist",
) -> ImpasseReport:
    return ImpasseReport(
        failing_test=failing_test,
        assertion_output=assertion_output,
        approaches=approaches,
        unsatisfiable_criterion=unsatisfiable_criterion,
        what_would_satisfy=what_would_satisfy,
    )


def verdict(
    v: Verdict = Verdict.INCONCLUSIVE,
    *,
    brief: str = "the brief, rewritten",
    findings: str | None = None,
    rationale: str = "because",
) -> EditorVerdict:
    """A `revise` carries the brief to restart against; a terminal verdict may not carry one at all.

    The builder mirrors the invariant rather than working around it — `verdict(Verdict.REVISE)` gets
    a brief because a `revise` without one is not a thing that can exist.

    `findings=None` means the Editor left them alone, which is the common case: most revisions
    rewrite the bar, not what was learned about the repo.
    """
    revising = v is Verdict.REVISE
    return EditorVerdict(
        verdict=v,
        revised_brief=Brief(body=brief) if revising else None,
        revised_findings=Findings(body=findings) if findings is not None else None,
        rationale=rationale,
    )


def graph_of(edges: Mapping[str, Iterable[str]]) -> IssueGraph:
    """`graph_of({"01": [], "02": ["01"]})` — the shape most tests need, without the ceremony."""
    return IssueGraph(
        sub_issues={
            SubIssueId(id): SubIssue(
                id=SubIssueId(id),
                title=f"sub-issue {id}",
                blocked_by=frozenset(SubIssueId(b) for b in blockers),
            )
            for id, blockers in edges.items()
        }
    )
