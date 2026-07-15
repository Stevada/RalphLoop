"""Running an agent in a worktree, and collecting what it cannot report about itself.

Every session below is a real subprocess in a real worktree. The telemetry is checked against what
git actually says, not against what the agent claimed.
"""

from __future__ import annotations

import pytest

from ralph.adapters.git import GitCli
from ralph.adapters.session import ImpasseParseError, SubprocessImplementer, parse_impasse
from ralph.domain import Outcome, SuiteResult, classify_implementer
from ralph.issues import Brief, Findings
from ralph.ports import Budget, SessionContext, Worktree
from tests.testbed import Behaviour, StandInAgent, TargetRepo

BRIEF, FINDINGS = Brief(body="make it work"), Findings(body="")
GENEROUS = Budget(wall_clock_s=60.0)


def context(wt: Worktree, budget: Budget = GENEROUS) -> SessionContext:
    return SessionContext(brief=BRIEF, findings=FINDINGS, worktree=wt, budget=budget)


def implementer(agent: StandInAgent, behaviour: Behaviour) -> SubprocessImplementer:
    return SubprocessImplementer(
        build_argv=lambda brief, findings, wt: agent.argv(
            behaviour, wt.branch.removeprefix("ralph/")
        )
    )


async def test_a_successful_session_reports_the_harnesss_facts_not_the_models(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await implementer(agent, Behaviour.SUCCEED).run(context(wt))

    assert t.exit_code == 0
    assert t.killed is None
    assert t.commits == 1  # counted by git, not claimed by the agent
    assert "feature_01.py" in t.diffstat
    assert t.wall_clock_s > 0
    assert t.impasse_report is None


async def test_a_session_that_committed_nothing_is_caught_by_the_commit_count(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """It exits 0 and says it is happy. Git says it did nothing, and git is the fact."""
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await implementer(agent, Behaviour.COMMIT_NOTHING).run(context(wt))

    assert t.exit_code == 0
    assert t.commits == 0
    assert classify_implementer(t, SuiteResult(True, "", 0.0)) is Outcome.IMPASSE


async def test_a_hanging_session_is_killed_on_the_wall_clock(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The bound that catches a *stuck* session. Its context would be flat, so no ceiling would
    ever have fired — only the clock stops it."""
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await implementer(agent, Behaviour.HANG).run(context(wt, Budget(wall_clock_s=1.0)))

    assert t.killed == "wall-clock"
    assert t.commits == 0
    assert classify_implementer(t, SuiteResult(False, "", 0.0)) is Outcome.INFRA_FAILED


async def test_the_impasse_sentinel_is_parsed_out_of_the_session(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    t = await implementer(agent, Behaviour.IMPASSE).run(context(wt))

    assert t.impasse_report is not None
    assert t.impasse_report.what_would_satisfy == "an API that exists"
    assert len(t.impasse_report.approaches) == 2
    assert t.impasse_report.approaches[0].abandoned_because == "there are none"
    assert classify_implementer(t, SuiteResult(False, "", 0.0)) is Outcome.IMPASSE


def test_no_sentinel_is_no_report() -> None:
    assert parse_impasse("I finished the work and it was fine.") is None


def test_an_unreadable_impasse_is_loud() -> None:
    """Silently ignoring it would classify a session that *told us it was stuck* as an undeclared
    impasse — dropping the model's claim and handing the Editor a story it never told. The failure
    would look exactly like a correctly-handled one."""
    with pytest.raises(ImpasseParseError):
        parse_impasse("<impasse>\n{not json at all}\n</impasse>")


def test_an_unterminated_sentinel_is_loud() -> None:
    with pytest.raises(ImpasseParseError, match="no </impasse>"):
        parse_impasse("<impasse>\n{}\n")
