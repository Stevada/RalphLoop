"""The merge queue, against real git.

The one claim worth testing hardest: the suite is re-run **in the worktree, after the rebase**, on
the prospective merge result. Run it anywhere else and you have verified a tree you are not
landing — a green integration branch that is broken, which is the failure the harness exists to
make impossible.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ralph.adapters.git import GitCli
from ralph.adapters.suite import SubprocessTestRunner, detect_test_cmd
from ralph.domain import SubIssue, SubIssueId, SubIssueState
from ralph.mergequeue import LandResult, MergeQueue
from ralph.ports import Worktree
from tests.fakes import FakeGit, FakeRunLog, FakeTestRunner
from tests.testbed import Behaviour, StandInAgent, TargetRepo


def sub(id: str) -> SubIssue:
    return SubIssue(id=SubIssueId(id), title=f"sub-issue {id}", blocked_by=frozenset())


def real_queue(repo: TargetRepo, log: FakeRunLog) -> tuple[MergeQueue, GitCli]:
    git = GitCli(repo=repo.path)
    runner = SubprocessTestRunner(cmd=detect_test_cmd(repo.path))
    return MergeQueue(git=git, runner=runner, integration="integration", log=log), git


def work(agent: StandInAgent, behaviour: Behaviour, tag: str, wt: Worktree) -> None:
    subprocess.run(agent.argv(behaviour, tag), cwd=wt.path, check=True, capture_output=True)


async def test_a_green_sub_issue_lands_and_the_history_stays_linear(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    log = FakeRunLog()
    queue, git = real_queue(repo, log)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    work(agent, Behaviour.SUCCEED, "01", wt)

    assert await queue.land(sub("01"), wt) is LandResult.LANDED

    assert (repo.path / "feature_01.py").exists()
    assert repo.git("log", "--merges", "--oneline", "integration") == ""
    assert [(e.kind, e.payload) for e in log.events()] == [("terminal", SubIssueState.LANDED)]


async def test_a_conflicting_sibling_is_a_rebase_conflict(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    log = FakeRunLog()
    queue, git = real_queue(repo, log)
    left = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    right = git.add_worktree("ralph/02", repo.path / ".worktrees" / "active" / "02", "integration")
    work(agent, Behaviour.CONFLICT, "01", left)
    work(agent, Behaviour.CONFLICT, "02", right)

    assert await queue.land(sub("01"), left) is LandResult.LANDED
    assert await queue.land(sub("02"), right) is LandResult.REBASE_CONFLICT

    assert repo.git("rev-parse", "integration") == repo.git("rev-parse", "ralph/01")
    assert log.events() == log.events()[:1]  # nothing was logged as landed for 02


async def test_the_suite_runs_on_the_prospective_merge_not_on_the_worktree_as_it_was(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The semantic conflict. Each sub-issue is green in isolation; together they are red. Only a
    suite run *after* the rebase can see it — and it is precisely what the Implementer could not
    have observed about itself.
    """
    log = FakeRunLog()
    queue, git = real_queue(repo, log)
    left = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    right = git.add_worktree("ralph/02", repo.path / ".worktrees" / "active" / "02", "integration")

    # 01 renames the function. 02 adds a test that calls it by its old name. Neither touches the
    # other's lines, so there is no textual conflict at all — and both are green alone.
    (left.path / "calculator.py").write_text("def plus(a: int, b: int) -> int:\n    return a + b\n")
    (left.path / "test_calculator.py").write_text(
        "from calculator import plus\n\n\ndef test_plus() -> None:\n    assert plus(1, 2) == 3\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=left.path, check=True)
    subprocess.run(["git", "commit", "-m", "rename add to plus"], cwd=left.path, check=True)

    (right.path / "test_extra.py").write_text(
        "from calculator import add\n\n\ndef test_add_again() -> None:\n    assert add(2, 2) == 4\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=right.path, check=True)
    subprocess.run(["git", "commit", "-m", "one more test for add"], cwd=right.path, check=True)

    assert await queue.land(sub("01"), left) is LandResult.LANDED
    assert await queue.land(sub("02"), right) is LandResult.SUITE_RED

    # The integration branch is exactly where 01 left it, and it is green.
    assert repo.git("rev-parse", "integration") == repo.git("rev-parse", "ralph/01")
    assert repo.run_suite() is True


async def test_the_land_aborts_if_the_base_repo_moved_out_from_under_it(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Fast-forwarding now would move a branch nobody asked us to move."""
    log = FakeRunLog()
    queue, git = real_queue(repo, log)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    work(agent, Behaviour.SUCCEED, "01", wt)
    repo.git("checkout", "main")

    assert await queue.land(sub("01"), wt) is LandResult.HEAD_MOVED

    assert log.events() == ()


async def test_a_refused_fast_forward_is_reported_never_papered_over() -> None:
    """Unreachable by construction — we just rebased onto integration, so integration is an
    ancestor. If git refuses anyway, something we believe about the repository is false. The one
    thing the queue must not do is reach for a merge commit, which would put an unverified tree on
    the integration branch: `git merge` does not fire the pre-commit hook.

    Driven with a fake git, because a real one cannot be made to refuse after a successful rebase —
    which is the point.
    """
    git = FakeGit(head="integration", ff_refuses={"ralph/01"})
    log = FakeRunLog()
    queue = MergeQueue(git=git, runner=FakeTestRunner(), integration="integration", log=log)
    wt = git.add_worktree("ralph/01", Path("/nowhere"), "integration")

    assert await queue.land(sub("01"), wt) is LandResult.FF_REFUSED

    assert git.merged == []
    assert log.events() == ()
