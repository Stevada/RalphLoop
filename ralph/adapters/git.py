"""Real git, against a real repository.

The one adapter with no plausible fake: a fake git that always says "merge succeeded" tests
nothing, and the merge queue is the trickiest code in the harness. Its tests run against a real
temporary repo.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ralph.ports import Worktree

DETACHED = "HEAD"
"""What `rev-parse --abbrev-ref HEAD` says when there is no branch. Fast-forwarding a detached
HEAD would move nothing and report success — the quietest way to lose a landed sub-issue."""


class GitError(RuntimeError):
    """A git command failed where the harness had no contingency for it. Never swallowed."""


def run_git(cwd: Path, *args: str) -> str:
    """Loud by construction. There is no `|| true` in this file."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd} ({proc.returncode}):\n{proc.stderr}")
    return proc.stdout.strip()


def _try_git(cwd: Path, *args: str) -> bool:
    """For the commands whose failure is a *result*, not an error: a merge can conflict, a
    fast-forward can be refused, a ref can simply not exist. Everything else goes through `run_git`
    and raises."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode == 0


def git_metadata(repo: Path) -> Path:
    """Where git writes when a session commits — the index, the objects, the refs — for `repo` and
    every worktree cut from it.

    **None of it is inside the worktree.** A linked worktree's `.git` is a pointer file into this
    directory, so an actor confined to its own checkout can rewrite every source file it was asked
    to and still not be able to record that it did. Asked of git rather than assembled as
    `repo / ".git"`, which is a file, not a directory, whenever the target repo is itself a
    worktree or was cloned with `--separate-git-dir`.
    """
    return Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


@dataclass(frozen=True, slots=True)
class GitCli:
    repo: Path

    def add_worktree(self, branch: str, at: Path, base: str) -> Worktree:
        at.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.repo, "worktree", "add", "-b", branch, str(at), base)
        return Worktree(path=at, branch=branch, base=base)

    def move_worktree(self, wt: Worktree, to: Path) -> Worktree:
        """`git worktree move`, not `shutil.move` — the checkout is registered in the base repo's
        `.git/worktrees`, and moving the directory behind git's back leaves a dangling registration
        that breaks the next `worktree add` at the old path.

        Raises rather than shrugging. A failure here is not cosmetic: the quarantined worktree is
        the evidence, and a human who is told it is at `failed/` must find it at `failed/`.
        """
        to.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.repo, "worktree", "move", str(wt.path), str(to))
        return Worktree(path=to, branch=wt.branch, base=wt.base)

    def discard_worktree(self, wt: Worktree) -> None:
        """`--force` because the tree is dirty by construction — uncommitted edits from a session
        that failed, artefacts from the suite run that preceded a landing — and plain `remove`
        refuses a dirty checkout. Removal comes first: git will not delete a branch that is still
        checked out somewhere.
        """
        run_git(self.repo, "worktree", "remove", "--force", str(wt.path))
        run_git(self.repo, "branch", "-D", wt.branch)

    def merge(self, wt: Worktree, onto: str) -> bool:
        """False on conflict, leaving git's conflict state exactly as it produced it.

        `--no-edit` because no session has a terminal: git opens an editor for the merge commit
        message on the merges that want one, and a merge that blocked on `vi` would hold the merge
        lock until the run was killed.
        """
        return _try_git(wt.path, "merge", "--no-edit", onto)

    def merge_finished(self, wt: Worktree) -> bool:
        """`MERGE_HEAD` is git's own record that a merge is still open; the commit that concludes
        one deletes it. Asking git beats asking the model, and beats counting commits: a session
        that resolved everything and stopped without committing leaves a branch carrying exactly the
        commits it had before, which is indistinguishable from success by any count.
        """
        return not _try_git(wt.path, "rev-parse", "--verify", "--quiet", "MERGE_HEAD")

    def merge_ff_only(self, branch: str) -> bool:
        """False when git **refuses**, which is the point. `git merge` does not fire the pre-commit
        hook, so a merge commit would put an unverified tree on the integration branch. The merge
        queue already re-ran the suite on the prospective merge result; if that result is not a
        fast-forward, the thing we verified is not the thing we would be landing."""
        return _try_git(self.repo, "merge", "--ff-only", branch)

    def commits_between(self, base: str, branch: str) -> int:
        return int(run_git(self.repo, "rev-list", "--count", f"{base}..{branch}"))

    def head_branch(self) -> str:
        branch = run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        if branch == DETACHED:
            raise GitError(f"{self.repo} is on a detached HEAD; there is no branch to land onto")
        return branch

    def dirty_files(self) -> tuple[str, ...]:
        """Uncommitted changes in the base checkout — tracked files only.

        Untracked files are not dirt: the harness itself writes `.scratch/<name>/run.jsonl` into the
        repo while it runs, and a pre-flight that refused its own run log would be unusable. Untracked
        files also do not stand in the way of a fast-forward, which is what this check is for.

        Not on the `Git` port. The scheduler and the merge queue never ask this — only the
        pre-flight does, and a Protocol is the list of what orchestration needs, not an inventory of
        what git can do.
        """
        status = run_git(
            self.repo, "status", "--porcelain", "--untracked-files=no", "--no-renames"
        )
        # Split on whitespace rather than slicing the two-column status code off the front:
        # `run_git` strips the output, so the *first* line has already lost its leading space and
        # a fixed slice would eat a character of its path. `--no-renames` keeps every line to one
        # path, so the split is unambiguous.
        return tuple(line.split(maxsplit=1)[1] for line in status.splitlines() if line.strip())
