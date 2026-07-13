"""The test bed: a real git repository, and a stand-in agent that is a real subprocess.

Neither of these is a fake. `tests/fakes.py` satisfies a Protocol in-process; these two do the
real thing in a temporary directory — real `git init`, real commits, real worktrees, real rebase
conflicts, a real suite that really goes red. The only thing the stand-in agent is not is
intelligent.

That is why every ticket after this one can be verified end-to-end **with no model at all**. A
fake git that always says "rebase succeeded" tests nothing; the merge queue is the trickiest code
in the harness and it deserves an adversary.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# The suite the throwaway repo ships with. Real pytest, run as a real subprocess, in the venv
# interpreter — the same one the harness itself will detect and run.
TEST_CMD: tuple[str, ...] = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")

IMPASSE_OPEN, IMPASSE_CLOSE = "<impasse>", "</impasse>"
"""The sentinel the Implementer emits when it cannot proceed. The body between the tags is JSON
keyed exactly to `ImpasseReport`'s fields; #08 owns the parser that turns it into one. Fixing the
wire format here — where the only writer lives — keeps the writer and the reader from drifting."""


class Behaviour(StrEnum):
    """What the stand-in agent has been told to do. Six shapes, and the harness must tell them
    apart: five of them exit in ways that look alike from the outside."""

    SUCCEED = "succeed"
    COMMIT_NOTHING = "commit-nothing"
    RED_SUITE = "red-suite"
    IMPASSE = "impasse"
    HANG = "hang"
    CONFLICT = "conflict"


def _git(cwd: Path, *args: str) -> str:
    """Fail loudly. A test bed that swallows a git error is worse than no test bed."""
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}:\n{proc.stderr}")
    return proc.stdout.strip()


@dataclass(frozen=True, slots=True)
class TargetRepo:
    """A throwaway repo shaped like one the harness would be pointed at: a real suite, a real
    issue graph under `.scratch/`, and HEAD on a non-protected branch."""

    path: Path
    integration_branch: str = "integration"

    @property
    def issues_dir(self) -> Path:
        return self.path / ".scratch" / "demo" / "issues"

    def git(self, *args: str) -> str:
        return _git(self.path, *args)

    def head(self, ref: str = "HEAD") -> str:
        return self.git("rev-parse", ref)

    def commit_count(self, ref: str = "HEAD") -> int:
        return int(self.git("rev-list", "--count", ref))

    def branch_exists(self, name: str) -> bool:
        return name in self.git("branch", "--format=%(refname:short)").splitlines()

    def add_worktree(self, sub_issue: str, base: str | None = None) -> Path:
        """A real `git worktree add`, at the path the harness uses."""
        wt = self.path / ".worktrees" / "active" / sub_issue
        self.git(
            "worktree",
            "add",
            "-b",
            f"ralph/{sub_issue}",
            str(wt),
            base or self.integration_branch,
        )
        return wt

    def run_suite(self, cwd: Path | None = None) -> bool:
        """Green or not. The suite result, not an exit code someone reported to us."""
        proc = subprocess.run(
            TEST_CMD, cwd=cwd or self.path, capture_output=True, text=True, check=False
        )
        return proc.returncode == 0


def make_target_repo(root: Path) -> TargetRepo:
    """Build the throwaway repo. Torn down with its tmp dir; nothing to clean up by hand."""
    repo = TargetRepo(path=root)
    root.mkdir(parents=True, exist_ok=True)

    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "testbed@ralph.invalid")
    _git(root, "config", "user.name", "Ralph Test Bed")
    _git(root, "config", "commit.gpgsign", "false")

    (root / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    (root / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"
    )
    # The line both CONFLICT agents rewrite. It exists on the base so they *modify* it rather
    # than both adding it — a plain content conflict, the kind a real rebase actually hits.
    (root / "shared.py").write_text('MARKER = "base"\n')
    (root / ".gitignore").write_text(".worktrees/\n.pytest_cache/\n__pycache__/\n")

    repo.issues_dir.mkdir(parents=True)
    (repo.issues_dir / "01-first.md").write_text(
        "# 01 — first\n\nStatus: ready\n\n## Acceptance criteria\n\n- [ ] It adds.\n"
    )
    (repo.issues_dir / "02-second.md").write_text(
        "# 02 — second\n\nStatus: ready\n\n## Acceptance criteria\n\n- [ ] It also adds.\n\n"
        "## Blocked by\n\n- #01\n"
    )

    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    _git(root, "checkout", "-b", repo.integration_branch)
    return repo


_AGENT_SOURCE = '''\
"""The stand-in agent. A real subprocess doing real work, scripted.

Invoked with a behaviour and a tag (the sub-issue it is pretending to be), in a worktree it
treats exactly as an Implementer would: it edits files there and commits them.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

IMPASSE_OPEN, IMPASSE_CLOSE = "<impasse>", "</impasse>"


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def commit(message: str) -> None:
    git("add", "-A")
    git("-c", "user.email=agent@ralph.invalid", "-c", "user.name=Stand-In", "commit", "-m", message)


def main() -> int:
    behaviour, tag = sys.argv[1], sys.argv[2]
    cwd = Path.cwd()

    if behaviour == "succeed":
        (cwd / f"feature_{tag}.py").write_text(f"VALUE = {tag!r}\\n")
        (cwd / f"test_feature_{tag}.py").write_text(
            f"from feature_{tag} import VALUE\\n\\n\\ndef test_value() -> None:\\n"
            f"    assert VALUE == {tag!r}\\n"
        )
        commit(f"feat({tag}): a real change, really committed")
        return 0

    if behaviour == "commit-nothing":
        print(f"[{tag}] I had a good think about it and decided the code was fine already.")
        return 0

    if behaviour == "red-suite":
        (cwd / f"test_broken_{tag}.py").write_text(
            "def test_broken() -> None:\\n    assert 1 == 2, 'the agent shipped this'\\n"
        )
        commit(f"feat({tag}): looks green to me")
        return 0

    if behaviour == "impasse":
        report = {
            "failing_test": f"test_feature_{tag}",
            "assertion_output": "AssertionError: the API does not exist",
            "approaches": [
                {"tried": "reading the docs", "abandoned_because": "there are none"},
                {"tried": "guessing", "abandoned_because": "that is the failure mode"},
            ],
            "unsatisfiable_criterion": f"the second acceptance criterion of {tag}",
            "what_would_satisfy": "an API that exists",
        }
        print(IMPASSE_OPEN)
        print(json.dumps(report, indent=2))
        print(IMPASSE_CLOSE)
        return 0

    if behaviour == "hang":
        print(f"[{tag}] thinking very hard", flush=True)
        time.sleep(3600)
        return 0  # unreachable: the harness kills us long before this

    if behaviour == "conflict":
        (cwd / "shared.py").write_text(f"MARKER = {tag!r}\\n")
        commit(f"feat({tag}): claim the shared line")
        return 0

    raise SystemExit(f"unknown behaviour: {behaviour!r}")


if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass(frozen=True, slots=True)
class StandInAgent:
    """A real executable on disk. `argv()` is what an Implementer adapter runs."""

    script: Path

    def argv(self, behaviour: Behaviour, tag: str) -> tuple[str, ...]:
        return (sys.executable, str(self.script), behaviour.value, tag)


def make_stand_in_agent(root: Path) -> StandInAgent:
    """Written outside the repo, so the agent never commits itself."""
    script = root / "stand_in_agent.py"
    script.write_text(_AGENT_SOURCE)
    script.chmod(0o755)
    return StandInAgent(script=script)


def impasse_body(stdout: str) -> dict[str, object]:
    """The JSON between the sentinels. #08's parser will do this into an `ImpasseReport`; here it
    is only so a test can prove the agent really emitted one."""
    start = stdout.index(IMPASSE_OPEN) + len(IMPASSE_OPEN)
    end = stdout.index(IMPASSE_CLOSE)
    parsed: dict[str, object] = json.loads(stdout[start:end])
    return parsed
