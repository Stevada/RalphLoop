"""The test bed proves itself.

A fixture that lies about what it set up is worse than no fixture: every ticket below trusts these
two, so the conflict really has to conflict and the hang really has to hang.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.testbed import Behaviour, StandInAgent, TargetRepo, impasse_body

# --- the throwaway repo -------------------------------------------------------------------------


def test_the_repo_is_a_real_git_repo_with_a_real_suite_and_a_real_graph(repo: TargetRepo) -> None:
    assert (repo.path / ".git").is_dir()
    assert repo.commit_count() == 1
    assert repo.git("rev-parse", "--abbrev-ref", "HEAD") == "integration"
    assert repo.run_suite() is True

    issues = sorted(p.name for p in repo.issues_dir.glob("*.md"))
    assert issues == ["01-first.md", "02-second.md"]
    assert "Status: ready" in (repo.issues_dir / "01-first.md").read_text()
    assert "#01" in (repo.issues_dir / "02-second.md").read_text()


def test_head_is_not_on_a_protected_branch(repo: TargetRepo) -> None:
    """`ralph validate` refuses to run on `main`. The fixture must not hand it a reason to."""
    assert repo.git("rev-parse", "--abbrev-ref", "HEAD") not in {"main", "master"}


def test_the_suite_really_goes_red_when_the_code_is_wrong(repo: TargetRepo) -> None:
    """Otherwise `silent-red` could never be observed, and the harness's central claim — the
    suite result, not the exit code, is the outcome — would rest on a suite that always passes."""
    (repo.path / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a * b\n")
    assert repo.run_suite() is False


def test_one_test_does_not_see_another_repos_state(repo: TargetRepo) -> None:
    (repo.path / "left_behind.txt").write_text("this must not survive")
    repo.git("add", "-A")
    repo.git("commit", "-m", "litter")
    repo.add_worktree("99")
    assert repo.commit_count() == 2


def test_the_next_test_gets_a_clean_repo(repo: TargetRepo) -> None:
    """The other half of the isolation assertion — and it must run against a fresh fixture, not
    the one the test above littered in."""
    assert not (repo.path / "left_behind.txt").exists()
    assert repo.commit_count() == 1
    assert not repo.branch_exists("ralph/99")


# --- the stand-in agent -------------------------------------------------------------------------


def _run(
    agent: StandInAgent, behaviour: Behaviour, tag: str, cwd: Path, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        agent.argv(behaviour, tag),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_the_agent_is_a_real_subprocess_working_in_the_worktree(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Not an in-process fake. It commits to the worktree's branch, and the integration branch
    knows nothing about it until the merge queue says so."""
    wt = repo.add_worktree("01")

    proc = _run(agent, Behaviour.SUCCEED, "01", cwd=wt)

    assert proc.returncode == 0
    assert (wt / "feature_01.py").exists()
    assert not (repo.path / "feature_01.py").exists()
    assert repo.commit_count("ralph/01") == 2
    assert repo.commit_count("integration") == 1


def test_succeed_leaves_the_suite_green(repo: TargetRepo, agent: StandInAgent) -> None:
    wt = repo.add_worktree("01")
    _run(agent, Behaviour.SUCCEED, "01", cwd=wt)
    assert repo.run_suite(cwd=wt) is True


def test_commit_nothing_commits_nothing(repo: TargetRepo, agent: StandInAgent) -> None:
    """Exit 0, a cheerful message, and not one commit. The prototype called this a skip; the
    harness calls it `silent-red`."""
    wt = repo.add_worktree("01")

    proc = _run(agent, Behaviour.COMMIT_NOTHING, "01", cwd=wt)

    assert proc.returncode == 0
    assert repo.commit_count("ralph/01") == 1
    assert repo.git("status", "--porcelain") == ""


def test_red_suite_commits_and_leaves_the_suite_red(repo: TargetRepo, agent: StandInAgent) -> None:
    """The dangerous one: it exits 0, it committed, and it is broken. Only running the suite
    catches it."""
    wt = repo.add_worktree("01")

    proc = _run(agent, Behaviour.RED_SUITE, "01", cwd=wt)

    assert proc.returncode == 0
    assert repo.commit_count("ralph/01") == 2
    assert repo.run_suite(cwd=wt) is False


def test_impasse_emits_the_sentinel_with_a_report(repo: TargetRepo, agent: StandInAgent) -> None:
    wt = repo.add_worktree("01")

    proc = _run(agent, Behaviour.IMPASSE, "01", cwd=wt)

    report = impasse_body(proc.stdout)
    assert report["unsatisfiable_criterion"] == "the second acceptance criterion of 01"
    assert report["what_would_satisfy"] == "an API that exists"
    assert isinstance(report["approaches"], list)
    assert len(report["approaches"]) == 2
    assert repo.commit_count("ralph/01") == 1


def test_hang_really_outlives_a_short_wall_clock_bound(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The bound has to have something to bite. A "hang" that returns in 200ms would let the
    wall-clock backstop pass its tests while being entirely broken."""
    wt = repo.add_worktree("01")

    with pytest.raises(subprocess.TimeoutExpired):
        _run(agent, Behaviour.HANG, "01", cwd=wt, timeout=1.0)


def test_conflict_really_conflicts_with_a_real_sibling_branch(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Two agents, two worktrees, one line. The first lands; the second cannot rebase onto it.

    This is the fixture behind `integration-failed`, and it is the one most worth distrusting:
    a "conflict" mode that quietly rebases clean would make the merge queue's hardest path
    untested while every test stayed green.
    """
    left, right = repo.add_worktree("01"), repo.add_worktree("02")
    _run(agent, Behaviour.CONFLICT, "01", cwd=left)
    _run(agent, Behaviour.CONFLICT, "02", cwd=right)

    # 01 lands first, fast-forward.
    repo.git("merge", "--ff-only", "ralph/01")
    assert (repo.path / "shared.py").read_text() == "MARKER = '01'\n"

    rebase = subprocess.run(
        ["git", "rebase", "integration"],
        cwd=right,
        capture_output=True,
        text=True,
        check=False,
    )

    assert rebase.returncode != 0
    assert "CONFLICT" in rebase.stdout + rebase.stderr
    assert "shared.py" in rebase.stdout + rebase.stderr


def test_two_succeeding_agents_do_not_conflict(repo: TargetRepo, agent: StandInAgent) -> None:
    """The control. If everything conflicted, the conflict test above would prove nothing."""
    left, right = repo.add_worktree("01"), repo.add_worktree("02")
    _run(agent, Behaviour.SUCCEED, "01", cwd=left)
    _run(agent, Behaviour.SUCCEED, "02", cwd=right)

    repo.git("merge", "--ff-only", "ralph/01")
    rebase = subprocess.run(
        ["git", "rebase", "integration"],
        cwd=right,
        capture_output=True,
        text=True,
        check=False,
    )

    assert rebase.returncode == 0
    assert repo.run_suite(cwd=right) is True
