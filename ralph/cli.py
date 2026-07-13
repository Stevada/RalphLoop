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

from ralph.adapters.filesystem import FilesystemIssueStore
from ralph.adapters.git import GitCli
from ralph.adapters.runlog import JsonlRunLog
from ralph.adapters.session import SubprocessImplementer
from ralph.adapters.suite import SubprocessTestRunner, detect_test_cmd, install_once
from ralph.domain import Notification
from ralph.mergequeue import MergeQueue
from ralph.ports import Budget, Worktree
from ralph.scheduler import DEFAULT_CONCURRENCY, RunReport, Scheduler

AGENT_CMD_ENV = "RALPH_AGENT_CMD"
SUB_ISSUE_PLACEHOLDER = "{sub_issue}"
BRANCH_PREFIX = "ralph/"


class NoAgent(RuntimeError):
    """No Implementer was configured. The harness will not invent one."""


def _agent_argv(worktree: Worktree) -> Sequence[str]:
    """The sub-issue's id is on its branch — the harness put it there. The agent needs no other
    channel to know which sub-issue it is working on."""
    template = os.environ.get(AGENT_CMD_ENV)
    if not template:
        raise NoAgent(f"set {AGENT_CMD_ENV} to the agent's command line")
    sub_issue = worktree.branch.removeprefix(BRANCH_PREFIX)
    return [arg.replace(SUB_ISSUE_PLACEHOLDER, sub_issue) for arg in shlex.split(template)]


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


async def run(
    repo: Path,
    issues: Path | None,
    budget: Budget | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> RunReport:
    repo = repo.resolve()
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
        implementer=SubprocessImplementer(
            build_argv=lambda brief, findings, wt: _agent_argv(wt)
        ),
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
        lines.append(f"\n  {e.sub_issue}  {e.outcome.value}")
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

    args = parser.parse_args(argv)
    report = asyncio.run(run(args.repo, args.issues, concurrency=args.parallel))
    print(render(report.notification))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
