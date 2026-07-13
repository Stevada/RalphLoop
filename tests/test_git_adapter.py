"""Real git, against a real repo. There is no other honest way to test this.

Every assertion here is one the merge queue's correctness rests on, and a fake git would pass all
of them while telling us nothing.
"""

from __future__ import annotations

import subprocess

import pytest

from ralph.adapters.git import GitCli, GitError
from tests.testbed import Behaviour, StandInAgent, TargetRepo


def test_add_worktree_cuts_a_real_checkout_off_the_base(repo: TargetRepo) -> None:
    git = GitCli(repo=repo.path)

    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    assert wt.path.is_dir()
    assert (wt.path / "calculator.py").exists()
    assert wt.base == "integration"
    assert repo.branch_exists("ralph/01")


def test_a_clean_rebase_succeeds(repo: TargetRepo, agent: StandInAgent) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    subprocess.run(agent.argv(Behaviour.SUCCEED, "01"), cwd=wt.path, check=True, capture_output=True)

    assert git.rebase(wt, "integration") is True


def test_a_conflicting_rebase_returns_false_and_leaves_no_rebase_in_progress(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """A half-finished rebase in a worktree is worse than a failed one: the Editor would read a
    tree with conflict markers in it that neither actor ever produced."""
    git = GitCli(repo=repo.path)
    left = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    right = git.add_worktree("ralph/02", repo.path / ".worktrees" / "active" / "02", "integration")
    for wt, tag in ((left, "01"), (right, "02")):
        subprocess.run(
            agent.argv(Behaviour.CONFLICT, tag), cwd=wt.path, check=True, capture_output=True
        )
    git.merge_ff_only("ralph/01")

    assert git.rebase(right, "integration") is False

    assert not (right.path / ".git").is_dir()  # a worktree's .git is a file, not a dir
    status = subprocess.run(
        ["git", "status"], cwd=right.path, capture_output=True, text=True, check=True
    )
    assert "rebase in progress" not in status.stdout
    assert "<<<<<<<" not in (right.path / "shared.py").read_text()


def test_merge_ff_only_fast_forwards_and_leaves_history_linear(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    subprocess.run(agent.argv(Behaviour.SUCCEED, "01"), cwd=wt.path, check=True, capture_output=True)

    assert git.merge_ff_only("ralph/01") is True

    merges = repo.git("log", "--merges", "--oneline", "integration")
    assert merges == ""


def test_merge_ff_only_refuses_rather_than_making_a_merge_commit(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The refusal is the feature. `git merge` does not fire the pre-commit hook, so a merge commit
    would put an unverified tree onto the integration branch — and the merge queue verified the
    *rebased* tree, which is not the one a merge commit would land."""
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")
    subprocess.run(agent.argv(Behaviour.SUCCEED, "01"), cwd=wt.path, check=True, capture_output=True)

    # Integration moves on independently, so `ralph/01` is no longer a fast-forward of it.
    (repo.path / "unrelated.py").write_text("X = 1\n")
    repo.git("add", "-A")
    repo.git("commit", "-m", "integration moves on")

    assert git.merge_ff_only("ralph/01") is False
    assert repo.git("log", "--merges", "--oneline", "integration") == ""


def test_commits_between_counts_only_the_sessions_work(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    git = GitCli(repo=repo.path)
    wt = git.add_worktree("ralph/01", repo.path / ".worktrees" / "active" / "01", "integration")

    assert git.commits_between("integration", "ralph/01") == 0

    subprocess.run(agent.argv(Behaviour.SUCCEED, "01"), cwd=wt.path, check=True, capture_output=True)
    assert git.commits_between("integration", "ralph/01") == 1


def test_head_branch_reports_the_branch(repo: TargetRepo) -> None:
    assert GitCli(repo=repo.path).head_branch() == "integration"


def test_head_branch_raises_on_a_detached_head(repo: TargetRepo) -> None:
    """Fast-forwarding a detached HEAD moves nothing and reports success — the quietest possible
    way to lose a landed sub-issue."""
    repo.git("checkout", "--detach")

    with pytest.raises(GitError, match="detached"):
        GitCli(repo=repo.path).head_branch()


def test_a_git_failure_is_never_swallowed(repo: TargetRepo) -> None:
    with pytest.raises(GitError):
        GitCli(repo=repo.path).commits_between("integration", "no-such-branch")
