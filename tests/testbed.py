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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ralph.adapters.runtime.implementer import SubprocessImplementer
from ralph.cli import RunOptions
from ralph.issues import Brief, Findings
from ralph.ports import Implementer, Worktree

# The suite the throwaway repo ships with. Real pytest, run as a real subprocess, in the venv
# interpreter — the same one the harness itself will detect and run.
TEST_CMD: tuple[str, ...] = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")

# Nothing to install; a real command that exits 0, since `install_cmd` is required.
INSTALL_CMD: tuple[str, ...] = (sys.executable, "-c", "")

BRANCH_PREFIX = "ralph/"

IMPASSE_OPEN, IMPASSE_CLOSE = "<impasse>", "</impasse>"
"""The sentinel the Implementer emits when it cannot proceed. The body between the tags is JSON
keyed exactly to `ImpasseReport`'s fields; #08 owns the parser that turns it into one. Fixing the
wire format here — where the only writer lives — keeps the writer and the reader from drifting."""


class Behaviour(StrEnum):
    """What the stand-in agent has been told to do. Ten shapes, and the harness must tell them
    apart: five of them exit in ways that look alike from the outside."""

    SUCCEED = "succeed"
    SLOW = "slow"  # succeeds, but takes long enough for a sibling to overtake it
    COMMIT_NOTHING = "commit-nothing"
    RED_SUITE = "red-suite"
    IMPASSE = "impasse"
    HANG = "hang"
    CONFLICT = "conflict"
    RENAMES_THE_API = "renames-the-api"
    CALLS_THE_API = "calls-the-api"
    """The two halves of a **semantic** conflict, and the pair only means anything together.

    Each is a correct, green, self-contained sub-issue: one renames `calculator.add` to `plus` and
    updates its test; the other adds a test that calls `add`. They touch **different files**, so
    there is no rebase conflict to catch them — git will merge them without a murmur, and the
    integration branch will be red.

    This is the failure `CONFLICT` cannot express. A textual conflict is git's to notice; a semantic
    one is nobody's, unless the suite is re-run on the prospective merge — which is the merge
    queue's entire reason for existing, and the thing these two exist to prove it does.
    """

    IMPASSE_ONCE = "impasse-once"
    """Declares an impasse the first time it is asked, and succeeds the second.

    The only behaviour that is not a pure function of its argv, and it exists for one reason: it is
    how a *cycle* becomes observable from outside. An Editor's `revise` is supposed to discard the
    work and restart the sub-issue clean — and a sub-issue restarted against a fresh worktree looks
    identical, from the harness's side, to one that was never restarted at all. Unless the agent
    remembers. It counts its own sessions in a file beside the script, outside the repo, so the
    count survives the worktree being destroyed — which is the very thing being tested.
    """


SLOW_S = 0.75
"""How long `SLOW` takes. Long enough that a `SUCCEED` sibling dispatched at the same moment
finishes and lands first — which is how "no wave barrier" is observed rather than asserted."""

LEDGER_ENV = "RALPH_TESTBED_LEDGER"
"""Where the agent records that it started and stopped. Set by a test, read by the agent; the
harness knows nothing about it, which is the only way it can be evidence *about* the harness."""


def behaviour_spec(default: Behaviour, per_sub_issue: Mapping[str, Behaviour] | None = None) -> str:
    """One agent, different behaviour per sub-issue — the shape every quarantine test needs, because
    the whole claim is that one sub-issue can fail while its siblings land."""
    table = [f"{tag}={b.value}" for tag, b in sorted((per_sub_issue or {}).items())]
    return ",".join([*table, f"*={default.value}"])


def peak_concurrency(ledger: Path) -> int:
    """The high-water mark of agents alive at once, read off the ledger.

    Counted from the *append order* of the start/stop marks rather than their timestamps: appends
    from concurrent processes are ordered by when they happened, and that is exactly the fact we
    want — no clock resolution to argue about.
    """
    alive = peak = 0
    for mark in ledger.read_text().split():
        alive += 1 if mark.startswith("+") else -1
        peak = max(peak, alive)
    return peak


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

    def write_graph(self, edges: Mapping[str, Sequence[str]]) -> None:
        """Replace the issue graph with one of the test's own shape — `{"03": ["01", "02"]}` reads
        as "03 is blocked by 01 and 02".

        The default fixture is a chain, which can prove ordering and nothing about parallelism. A
        fan-out is what makes concurrency observable, and a diamond is what makes *draining* around
        a quarantined sub-issue observable. Both need this.

        Committed, not just written: the base repo's working tree must be clean enough that the
        merge queue's fast-forwards are not fighting stray edits in `.scratch/`.
        """
        for existing in self.issues_dir.glob("*.md"):
            existing.unlink()
        for id, blockers in edges.items():
            body = f"# {id} — sub-issue {id}\n\nStatus: ready\n\n## Acceptance criteria\n\n- [ ] It works.\n"
            if blockers:
                body += "\n## Blocked by\n\n" + "".join(f"- #{b}\n" for b in blockers)
            (self.issues_dir / f"{id}-sub.md").write_text(body)
        self.git("add", "-A")
        self.git("commit", "-m", "a graph of the test's own shape")


def make_options(**overrides: object) -> RunOptions:
    """Resolved CLI options a test can inject through `run(options=...)`."""
    base: dict[str, object] = {
        "issue_mode": "filesystem",
        "implementer": "codex",
        "editor": "claude",
        "protected": frozenset({"main", "master"}),
        "test_cmd": TEST_CMD,
        "install_cmd": INSTALL_CMD,
        "linear_api_key": None,
    }
    return RunOptions(**{**base, **overrides})  # type: ignore[arg-type]


def stand_in_implementer(agent: StandInAgent, behaviour: Behaviour | str) -> Implementer:
    """The scripted stand-in as an `Implementer`, for `run(implementer=…)`. The sub-issue id is on
    the worktree's branch — the harness put it there — so that is the only channel the agent needs."""

    def build_argv(brief: Brief, findings: Findings, worktree: Worktree) -> Sequence[str]:
        tag = worktree.branch.removeprefix(BRANCH_PREFIX)
        return agent.argv(behaviour, tag)

    return SubprocessImplementer(build_argv=build_argv)


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

Invoked with a behaviour spec and a tag (the sub-issue it is pretending to be), in a worktree it
treats exactly as an Implementer would: it edits files there and commits them.

The spec is either one behaviour for everyone (`succeed`) or a per-sub-issue table with a default
(`02=impasse,*=succeed`). A run in which one sub-issue fails and its siblings land needs the table;
that run is the entire claim of quarantine-and-drain.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

IMPASSE_OPEN, IMPASSE_CLOSE = "<impasse>", "</impasse>"
SLOW_S = 0.75


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def commit(message: str) -> None:
    git("add", "-A")
    git("-c", "user.email=agent@ralph.invalid", "-c", "user.name=Stand-In", "commit", "-m", message)


def resolve(spec: str, tag: str) -> str:
    if "=" not in spec:
        return spec
    table = dict(entry.split("=", 1) for entry in spec.split(","))
    return table.get(tag, table["*"])


def mark(sign: str, tag: str) -> None:
    """Append-only, one token per line. Two concurrent agents both writing here is the point."""
    ledger = os.environ.get("RALPH_TESTBED_LEDGER")
    if ledger:
        with open(ledger, "a") as f:
            f.write(f"{sign}{tag}\\n")


def main() -> int:
    spec, tag = sys.argv[1], sys.argv[2]
    behaviour = resolve(spec, tag)
    cwd = Path.cwd()

    mark("+", tag)
    try:
        return act(behaviour, tag, cwd)
    finally:
        mark("-", tag)


def sessions_so_far(tag: str) -> int:
    """How many times this agent has been asked about `tag`, counted in a file beside the script.

    Beside the *script*, which lives outside the repo — not in the worktree, which is destroyed on
    every revise. A counter that did not survive the discard could not observe the discard.
    """
    counter = Path(__file__).parent / f".sessions-{tag}"
    before = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(before + 1))
    return before


def act(behaviour: str, tag: str, cwd: Path) -> int:
    if behaviour == "slow":
        time.sleep(SLOW_S)
        behaviour = "succeed"

    if behaviour == "impasse-once":
        if sessions_so_far(tag):
            behaviour = "succeed"
        else:
            # A first attempt that got somewhere and then got stuck. The partial work is *committed*
            # on purpose: it is what makes "the Implementer's work is discarded" observable at all.
            # A first session that committed nothing would leave nothing to fail to discard.
            (cwd / f"partial_{tag}.py").write_text("# half an idea, abandoned\\n")
            commit(f"wip({tag}): as far as I got")
            behaviour = "impasse"

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
        # It says so, in as many words, and it is wrong. The failure report must carry both this
        # sentence and the red suite the harness observed; the two disagreeing is the signal.
        print(f"[{tag}] All tests pass. The implementation is complete.")
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

    if behaviour == "renames-the-api":
        # Green here, and green forever, as long as nobody else calls `add`.
        (cwd / "calculator.py").write_text("def plus(a: int, b: int) -> int:\\n    return a + b\\n")
        (cwd / "test_calculator.py").write_text(
            "from calculator import plus\\n\\n\\ndef test_plus() -> None:\\n"
            "    assert plus(1, 2) == 3\\n"
        )
        commit(f"feat({tag}): a better name for add")
        return 0

    if behaviour == "calls-the-api":
        # Also green here, because `add` still exists on the base this was cut from. Neither agent
        # can see the other; that is what makes it a semantic conflict rather than a mistake.
        (cwd / "test_caller.py").write_text(
            "from calculator import add\\n\\n\\ndef test_caller() -> None:\\n"
            "    assert add(2, 2) == 4\\n"
        )
        commit(f"feat({tag}): rely on add")
        return 0

    raise SystemExit(f"unknown behaviour: {behaviour!r}")


if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass(frozen=True, slots=True)
class StandInAgent:
    """A real executable on disk. `argv()` is what an Implementer adapter runs."""

    script: Path

    def argv(self, behaviour: Behaviour | str, tag: str) -> tuple[str, ...]:
        """`behaviour` is one shape for everyone, or a `behaviour_spec` table keyed by sub-issue."""
        return (sys.executable, str(self.script), str(behaviour), tag)


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
