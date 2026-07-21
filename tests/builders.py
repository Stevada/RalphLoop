"""Builders for harness values.

Not fakes — a fake satisfies a Protocol at a seam; a builder constructs a value. These exist so a
test can state the one field it is actually about and let the rest default to something plausible.
`telemetry(commits=0)` says "a session that committed nothing" and nothing else, which is exactly
what the undeclared-impasse test means.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ralph.harness import (
    Approach,
    EditorVerdict,
    ImpasseReport,
    Killed,
    SessionTelemetry,
    SuiteResult,
    Verdict,
)
from ralph.issues import Findings, IssueGraph, Spec, SubIssue, SubIssueId


def telemetry(
    *,
    exit_code: int = 0,
    killed: Killed | None = None,
    consumed_tokens: int = 50_000,
    auto_compactions: int = 0,
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
        consumed_tokens=consumed_tokens,
        auto_compactions=auto_compactions,
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
    spec: str = "the spec, rewritten",
    findings: str | None = None,
    rationale: str = "because",
) -> EditorVerdict:
    """A `revise` carries the spec to restart against; a terminal verdict may not carry one at all.

    The builder mirrors the invariant rather than working around it — `verdict(Verdict.REVISE)` gets
    a spec because a `revise` without one is not a thing that can exist.

    `findings=None` means the Editor left them alone, which is the common case: most revisions
    rewrite the bar, not what was learned about the repo.
    """
    revising = v is Verdict.REVISE
    return EditorVerdict(
        verdict=v,
        revised_spec=Spec(body=spec) if revising else None,
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
