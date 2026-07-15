"""The composition root. **The only module that names a concrete adapter.**

Nothing downstream of here knows whether the Implementer is Codex, Copilot, or a scripted stand-in
that does not think at all — which is exactly why the stand-in can prove the whole system works
with no model in the loop.

An Implementer, at this layer, is an **argv**. `RALPH_AGENT_CMD` is that argv; Codex and Copilot
(#08, #10) become two more of them, chosen by `RALPH_IMPLEMENTER`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from ralph.adapters.claude_editor import ClaudeCodeEditor, claude_sdk_session
from ralph.adapters.codex import codex_implementer
from ralph.adapters.copilot import copilot_editor, copilot_implementer
from ralph.adapters.filesystem import FilesystemIssueStore, IssueParseError
from ralph.adapters.git import GitCli, run_git
from ralph.adapters.session import SubprocessImplementer
from ralph.adapters.suite import (
    NoSuiteFound,
    SubprocessTestRunner,
    detect_test_cmd,
    install_once,
)
from ralph.domain import (
    CycleLedger,
    GraphError,
    IssueGraph,
    Notification,
    Refusal,
    RepoFacts,
    SubIssueId,
    SubIssueState,
    build_order,
    refusals,
)
from ralph.mergequeue import MergeQueue
from ralph.ports import Budget, Editor, Implementer, Worktree
from ralph.runlog import JsonlRunLog
from ralph.scheduler import DEFAULT_CONCURRENCY, RunReport, Scheduler

AGENT_CMD_ENV = "RALPH_AGENT_CMD"
IMPLEMENTER_ENV = "RALPH_IMPLEMENTER"
EDITOR_ENV = "RALPH_EDITOR"
PROTECTED_ENV = "RALPH_PROTECTED_BRANCHES"
SUB_ISSUE_PLACEHOLDER = "{sub_issue}"
BRANCH_PREFIX = "ralph/"

DEFAULT_PROTECTED = ("main", "master")
PRE_COMMIT_CONFIGS = (".pre-commit-config.yaml", ".pre-commit-config.yml")

CODEX = "codex"
CLAUDE = "claude"
COPILOT = "copilot"


class NoAgent(RuntimeError):
    """No Implementer was configured. The harness will not invent one."""


class Refused(RuntimeError):
    """The pre-flight refused the run. Raised, not printed — `ralph run` does the same checks
    `ralph validate` does, and a check that only fires when a human remembers to ask for it is a
    check the run does not have."""


def _agent_argv(worktree: Worktree) -> Sequence[str]:
    """The sub-issue's id is on its branch — the harness put it there. The agent needs no other
    channel to know which sub-issue it is working on."""
    template = os.environ.get(AGENT_CMD_ENV)
    if not template:
        raise NoAgent(f"set {AGENT_CMD_ENV} to the agent's command line")
    sub_issue = worktree.branch.removeprefix(BRANCH_PREFIX)
    return [arg.replace(SUB_ISSUE_PLACEHOLDER, sub_issue) for arg in shlex.split(template)]


def editor_of(repo: Path) -> Editor | None:
    """Which model adjudicates — or **whether one does at all**.

    `None` is not a null Editor. It means *there is no Editor in this run*: failures quarantine on
    the Implementer's own outcome and the run drains around them. A null Editor returning no verdict
    would classify `infra-failed` and misreport every impasse in the run as a harness crash.

    The Editor and the Implementer should not be the same model on the same failure — an Editor
    adjudicating an impasse declared by *itself* is the least independent sensor the system could
    have. Nothing here enforces that; it is why two CLIs back each role.
    """
    named = os.environ.get(EDITOR_ENV)
    if named is None:
        return None
    if named == CLAUDE:
        return ClaudeCodeEditor(open_session=claude_sdk_session, suite=detect_test_cmd(repo))
    if named == COPILOT:
        # Read-only, but guaranteed by Copilot's own permission engine rather than by a function
        # this harness owns and tests. Weaker on purpose, and worth knowing here at the point of
        # choosing: see `CopilotEditor`. It buys independence — an Editor that is not the model
        # that just failed.
        return copilot_editor(suite=detect_test_cmd(repo))
    raise NoAgent(f"{EDITOR_ENV}={named!r} names no Editor. Known: {CLAUDE}, {COPILOT}.")


def implementer() -> Implementer:
    """Which model implements — the one decision only this module is allowed to make.

    Unset means *the argv in `RALPH_AGENT_CMD`*: the stand-in agent, or any other command-line
    agent. It is bounded on the clock alone, because it publishes no context signal to meter.
    `codex` is the first Implementer that does. Copilot joins it in #10.
    """
    named = os.environ.get(IMPLEMENTER_ENV)
    if named is None:
        return SubprocessImplementer(build_argv=lambda brief, findings, wt: _agent_argv(wt))
    if named == CODEX:
        return codex_implementer()
    if named == COPILOT:
        return copilot_implementer()
    raise NoAgent(f"{IMPLEMENTER_ENV}={named!r} names no Implementer. Known: {CODEX}, {COPILOT}.")


def find_issues_dir(repo: Path, given: Path | None) -> Path:
    """`.scratch/<phase>/issues/` is discovered when no path is given — and an ambiguous discovery
    is an error, not a guess. Two phases in flight means the human must say which."""
    if given is not None:
        return given
    candidates = sorted((repo / ".scratch").glob("*/issues"))
    if not candidates:
        raise FileNotFoundError(f"no .scratch/<phase>/issues under {repo}")
    if len(candidates) > 1:
        names = ", ".join(str(c.relative_to(repo)) for c in candidates)
        raise FileNotFoundError(f"several issue directories under {repo}: {names}. Name one.")
    return candidates[0]


def protected_branches() -> frozenset[str]:
    named = os.environ.get(PROTECTED_ENV)
    return frozenset(shlex.split(named) if named else DEFAULT_PROTECTED)


def _pre_commit(repo: Path) -> tuple[str | None, bool]:
    """Whether this repo asks for pre-commit, and whether it actually got it.

    The hook path comes from git rather than from `.git/hooks/`, because a repo may move it with
    `core.hooksPath` and a run in a worktree does not have a `.git` directory at all.
    """
    config = next((c for c in PRE_COMMIT_CONFIGS if (repo / c).exists()), None)
    if config is None:
        return None, False
    hook = repo / run_git(repo, "rev-parse", "--git-path", "hooks/pre-commit")
    return config, hook.exists()


def _read_graph(repo: Path, issues: Path | None) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]:
    return FilesystemIssueStore(issues_dir=find_issues_dir(repo, issues)).read_graph()


def facts_about(repo: Path, issues: Path | None) -> RepoFacts:
    """Ask the world the five questions, and hand the answers to a rule that cannot ask anything.

    Each `except` is narrow and each keeps the raiser's own message: `IssueParseError` already says
    exactly which file has no acceptance criteria, and `GraphError` already names the cycle. A
    pre-flight that rephrased them would be a second, worse copy of a sentence that is already right.
    """
    git = GitCli(repo=repo)
    config, installed = _pre_commit(repo)

    suite_error: str | None = None
    try:
        detect_test_cmd(repo)
    except NoSuiteFound as exc:
        suite_error = str(exc)

    graph_error: str | None = None
    try:
        _read_graph(repo, issues)
    except (IssueParseError, GraphError, FileNotFoundError) as exc:
        graph_error = str(exc)

    return RepoFacts(
        head_branch=git.head_branch(),
        protected=protected_branches(),
        dirty=git.dirty_files(),
        suite_error=suite_error,
        graph_error=graph_error,
        pre_commit_config=config,
        pre_commit_installed=installed,
    )


def validate(repo: Path, issues: Path | None = None) -> tuple[Refusal, ...]:
    return refusals(facts_about(repo.resolve(), issues))


def render_refusals(found: tuple[Refusal, ...]) -> str:
    if not found:
        return "ready to run."
    lines = [f"refusing to run ({len(found)}):"]
    lines += [f"\n  {r.check.value}\n    {r.reason}" for r in found]
    return "\n".join(lines)


def render_plan(repo: Path, issues: Path | None) -> str:
    """What `--dry-run` prints: the graph as the harness reads it, and the order it would work in.

    The cheapest possible dogfood — it parses every sub-issue, resolves every edge, and proves the
    graph is acyclic, and it costs nothing to run because no session is ever opened.
    """
    graph, states = _read_graph(repo.resolve(), issues)
    edges = sum(len(sub.blocked_by) for sub in graph.sub_issues.values())
    lines = [f"{len(graph.sub_issues)} sub-issues, {edges} edges, no cycle."]

    for n, wave in enumerate(build_order(graph, states), start=1):
        lines.append(f"  wave {n}: {', '.join(wave)}")

    # Named, because their absence from the waves above is otherwise indistinguishable from a
    # sub-issue the harness failed to see at all.
    idle = sorted(id for id, state in states.items() if state is not SubIssueState.READY)
    if idle:
        lines.append(
            "  not dispatched: " + ", ".join(f"{id} ({states[id].value})" for id in idle)
        )
    return "\n".join(lines)


async def run(
    repo: Path,
    issues: Path | None,
    budget: Budget | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    editor: Editor | None = None,
) -> RunReport:
    """An explicit `editor` overrides `RALPH_EDITOR` — that is the seam the tests inject a stub
    through, and the reason no test in the suite calls Opus."""
    repo = repo.resolve()

    # The same checks `ralph validate` runs, and they are not advisory. A run that starts on `main`
    # has already done the damage by the time anybody reads the warning it printed.
    found = validate(repo, issues)
    if found:
        raise Refused(render_refusals(found))

    git = GitCli(repo=repo)
    integration = git.head_branch()

    # Detected once, in the base checkout, before anything is dispatched. A repo whose suite the
    # harness cannot find is a fatal error here, not a green SuiteResult later.
    runner = SubprocessTestRunner(cmd=detect_test_cmd(repo))
    await install_once(repo)

    scheduler = Scheduler(
        repo=repo,
        git=git,
        store=FilesystemIssueStore(issues_dir=find_issues_dir(repo, issues)),
        run_log=JsonlRunLog(path=repo / ".scratch" / "run.jsonl"),
        runner=runner,
        implementer=implementer(),
        editor=editor if editor is not None else editor_of(repo),
        merge_queue=MergeQueue(git=git, runner=runner, integration=integration),
        integration=integration,
        budget=budget or Budget(),
        concurrency=concurrency,
    )
    return await scheduler.run()


def render(n: Notification) -> str:
    """**One** notification, at the end.

    The bar: *if you cannot tell from this alone whether to spend your first ten minutes reading a
    diff or rewriting a PRD, it has failed.* So each escalation leads with what kind of failure it
    was, says what it is holding up, and — where the model left one — quotes the criterion it
    believes it cannot satisfy. Most urgent first; there is no scrolling to find the important one.
    """
    lines = [f"landed: {', '.join(n.landed) if n.landed else 'nothing'}"]
    if not n.escalations:
        return lines[0]

    lines.append(f"\nneeds a human ({len(n.escalations)}), most urgent first:")
    for e in n.escalations:
        # The cycle count only earns a line when it is not 1. A sub-issue the Editor rewrote twice
        # and which still failed is a different animal from one that failed on first contact, and
        # the difference should be visible without opening the run log.
        spent = (
            f"  (cycle {e.report.cycles} of {CycleLedger.MAX_CYCLES})"
            if e.report.cycles > 1
            else ""
        )
        lines.append(f"\n  {e.sub_issue}  {e.outcome.value}{spent}")
        if e.stranded:
            lines.append(f"    holding up: {', '.join(e.stranded)}")
        if e.report.claim is not None:
            lines.append(f"    it says: {e.report.claim.unsatisfiable_criterion}")
            lines.append(f"    would need: {e.report.claim.what_would_satisfy}")
        if e.report.integration_detail is not None:
            lines.append(f"    the merge queue: {e.report.integration_detail}")
        commits = e.report.telemetry.commits
        lines.append(
            f"    harness: {commits} commit{'' if commits == 1 else 's'}, "
            f"suite {'green' if e.report.suite.green else 'red'}, "
            f"worktree preserved at .worktrees/failed/{e.sub_issue}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ralph")
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="run the issue graph to completion")
    runner.add_argument("repo", type=Path)
    runner.add_argument("issues", type=Path, nargs="?", default=None)
    runner.add_argument(
        "-j",
        "--parallel",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="how many sub-issues may run at once. They still land one at a time.",
    )
    runner.add_argument(
        "--dry-run",
        action="store_true",
        help="read the graph and print the build order. No session is opened.",
    )

    checker = sub.add_parser("validate", help="refuse a run this repo is not ready for")
    checker.add_argument("repo", type=Path)
    checker.add_argument("issues", type=Path, nargs="?", default=None)

    args = parser.parse_args(argv)

    if args.command == "validate":
        found = validate(args.repo, args.issues)
        print(render_refusals(found))
        return 1 if found else 0

    if args.dry_run:
        print(render_plan(args.repo, args.issues))
        return 0

    report = asyncio.run(run(args.repo, args.issues, concurrency=args.parallel))
    print(render(report.notification))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
