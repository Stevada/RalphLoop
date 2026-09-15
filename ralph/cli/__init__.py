"""The composition root. **The only place that names a concrete adapter.**

Nothing downstream of here knows whether the Implementer is Codex, Copilot, or a scripted stand-in
that does not think at all — which is exactly why the stand-in can prove the whole system works
with no model in the loop.

An Implementer, at this layer, is an **argv**. Codex and Copilot are chosen here from CLI arguments.

Telling a human what happened is `presentation.py`, next door; it is re-exported here so that
`ralph.cli` stays the one import path a caller needs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from ralph.adapters.commands import DescriptorCommandError
from ralph.adapters.git import GitCli, git_metadata, run_git
from ralph.adapters.suite import SubprocessTestRunner, install_once
from ralph.cli.actors import (
    IssueSourceError,
    actor_runtime_error,
    issue_store,
    parent_issue_name,
    command_source_for,
    editor_of,
    implementer_of,
    integrator_of,
)
from ralph.cli.actors import NoActor as NoActor
from ralph.cli.actors import validate_actors as validate_actors
from ralph.cli.options import (
    DEFAULT_LOG_LEVEL,
    DEFAULT_PROTECTED,
    _add_option_flags,
)
from ralph.cli.options import ENV_FILE as ENV_FILE
from ralph.cli.options import RunOptions as RunOptions
from ralph.cli.options import _load_env as _load_env
from ralph.cli.options import _options_for as _options_for
from ralph.cli.presentation import (
    NarratedRunLog,
    Readiness,
    render_readiness,
)
from ralph.cli.presentation import render as render
from ralph.cli.presentation import render_refusals as render_refusals

from ralph.harness import (
    Refusal,
    RepoFacts,
    build_order,
    refusals,
)
from ralph.issues import GraphError, IssueGraph, SubIssueId, SubIssueState
from ralph.issues.filesystem import IssueParseError
from ralph.issues.linear import LinearApiError, LinearIssueStoreError
from ralph.mergegate import MergeGate
from ralph.ports import (
    Budget,
    CommandSource,
    Editor,
    Implementer,
    Integrator,
    RepoCommands,
)
from ralph.runlog import JsonlRunLog
from ralph.scheduler import RunReport, Scheduler
from ralph.transcripts import FileTranscripts

log = logging.getLogger("ralph")

PRE_COMMIT_CONFIGS = (".pre-commit-config.yaml", ".pre-commit-config.yml")


class Refused(RuntimeError):
    """The pre-flight refused the run. Raised, not printed — `ralph run` does the same checks
    `ralph validate` does, and a check that only fires when a human remembers to ask for it is a
    check the run does not have."""


def _pre_commit(repo: Path) -> tuple[str | None, bool]:
    """Whether this repo asks for pre-commit, and whether it actually got it.

    The hook path comes from git rather than from `.git/hooks/`, because a repo may move it with
    `core.hooksPath` and a run in a worktree does not have a `.git` directory at all.
    """
    pre_commit_config = next((c for c in PRE_COMMIT_CONFIGS if (repo / c).exists()), None)
    if pre_commit_config is None:
        return None, False
    hook = repo / run_git(repo, "rev-parse", "--git-path", "hooks/pre-commit")
    return pre_commit_config, hook.exists()


def _read_graph(
    repo: Path, issue_source: str | None, options: RunOptions
) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]:
    return issue_store(repo, issue_source, options).read_graph()


def _discover_commands(
    repo: Path, command_source: CommandSource
) -> tuple[RepoCommands | None, str | None]:
    try:
        return command_source.discover(repo), None
    except DescriptorCommandError as exc:
        return None, str(exc)


def _facts_and_commands(
    repo: Path,
    issue_source: str | None,
    options: RunOptions,
    command_source: CommandSource | None = None,
    implementer: Implementer | None = None,
    editor: Editor | None = None,
    integrator: Integrator | None = None,
) -> tuple[RepoFacts, RepoCommands | None]:
    """Ask the world the pre-flight questions, and hand the answers to a rule that cannot ask anything.

    Each `except` is narrow and each keeps the raiser's own message: `IssueParseError` already says
    exactly which file has no acceptance criteria, and `GraphError` already names the cycle. A
    pre-flight that rephrased them would be a second, worse copy of a sentence that is already right.
    """
    git = GitCli(repo=repo)
    pre_commit_config, installed = _pre_commit(repo)
    commands, command_error = _discover_commands(repo, command_source or command_source_for())

    source_error: str | None = None
    graph_error: str | None = None
    try:
        _read_graph(repo, issue_source, options)
    except (IssueParseError, GraphError, LinearIssueStoreError) as exc:
        graph_error = str(exc)
    except (FileNotFoundError, IssueSourceError, LinearApiError) as exc:
        source_error = str(exc)

    return (
        RepoFacts(
            head_branch=git.head_branch(),
            protected=options.protected,
            dirty=git.dirty_files(),
            has_test_command=commands is not None and bool(commands.test),
            command_error=command_error,
            actor_runtime_error=actor_runtime_error(options, implementer, editor, integrator),
            source_error=source_error,
            graph_error=graph_error,
            pre_commit_config=pre_commit_config,
            pre_commit_installed=installed,
        ),
        commands,
    )


def facts_about(
    repo: Path,
    issue_source: str | None,
    options: RunOptions,
    command_source: CommandSource | None = None,
) -> RepoFacts:
    facts, _ = _facts_and_commands(repo, issue_source, options, command_source)
    return facts


def validate(
    repo: Path,
    issue_source: str | None = None,
    options: RunOptions | None = None,
    command_source: CommandSource | None = None,
    implementer: Implementer | None = None,
    editor: Editor | None = None,
    integrator: Integrator | None = None,
) -> tuple[Refusal, ...]:
    return readiness(
        repo, issue_source, options, command_source, implementer, editor, integrator
    ).refusals


def readiness(
    repo: Path,
    issue_source: str | None = None,
    options: RunOptions | None = None,
    command_source: CommandSource | None = None,
    implementer: Implementer | None = None,
    editor: Editor | None = None,
    integrator: Integrator | None = None,
) -> Readiness:
    """An actor passed here is one this run will not construct, so its runtime is not checked."""
    options = options or _options_for()
    validate_actors(options)
    facts, commands = _facts_and_commands(
        repo.resolve(), issue_source, options, command_source, implementer, editor, integrator
    )
    return Readiness(
        refusals=refusals(facts),
        commands=commands if commands is not None and commands.test else None,
    )


def render_plan(
    repo: Path,
    issue_source: str | None = None,
    options: RunOptions | None = None,
) -> str:
    """What `--dry-run` prints: the graph as the harness reads it, and the order it would work in.

    The cheapest possible dogfood — it parses every sub-issue, resolves every edge, and proves the
    graph is acyclic, and it costs nothing to run because no session is ever opened.
    """
    options = options or _options_for()
    validate_actors(options)
    graph, states = _read_graph(repo.resolve(), issue_source, options)
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
    issue_source: str | None = None,
    *,
    budget: Budget | None = None,
    implementer: Implementer | None = None,
    editor: Editor | None = None,
    integrator: Integrator | None = None,
    options: RunOptions | None = None,
    command_source: CommandSource | None = None,
    sequential: bool = False,
) -> RunReport:
    """Explicit `implementer`/`editor` override the ones the resolved options name — those are the seams
    the tests inject the scripted stand-in and a stub Editor through, and the reason no test in the
    suite calls a model."""
    repo = repo.resolve()
    options = options or _options_for()
    validate_actors(options)

    # The same checks `ralph validate` runs, and they are not advisory. A run that starts on `main`
    # has already done the damage by the time anybody reads the warning it printed.
    ready = readiness(
        repo, issue_source, options, command_source, implementer, editor, integrator
    )
    if ready.refusals:
        raise Refused(render_refusals(ready.refusals))
    if ready.commands is None:
        raise AssertionError("readiness accepted a run without a test command")
    commands = ready.commands

    git = GitCli(repo=repo)
    integration_branch = git.head_branch()

    # Install runs once in the base checkout, before anything is dispatched.
    runner = SubprocessTestRunner(cmd=commands.test)
    if commands.install is not None:
        await install_once(repo, commands.install)

    store = issue_store(repo, issue_source, options)
    metadata = git_metadata(repo)
    selected_implementer = (
        implementer if implementer is not None else implementer_of(options, metadata)
    )
    selected_editor = editor if editor is not None else editor_of(options, commands.test)
    selected_integrator = (
        integrator if integrator is not None else integrator_of(options, metadata)
    )
    parent = parent_issue_name(repo, issue_source, options)
    scratch = repo / ".scratch" / parent if parent is not None else repo / ".scratch"
    session_budget = budget or Budget()
    scheduler = Scheduler(
        repo=repo,
        git=git,
        store=store,
        run_log=NarratedRunLog(JsonlRunLog(path=scratch / "run.jsonl")),
        transcripts=FileTranscripts(root=scratch / "transcripts"),
        implementer=selected_implementer,
        editor=selected_editor,
        merge_gate=MergeGate(
            git=git,
            runner=runner,
            integration_branch=integration_branch,
            integrator=selected_integrator,
            budget=session_budget,
        ),
        integration_branch=integration_branch,
        budget=session_budget,
        parent_issue_name=parent,
        sequential=sequential,
    )
    report = await scheduler.run()
    notification = render(report.notification, parent)
    try:
        await store.publish_notification(notification)
    except Exception:  # noqa: BLE001 — stdout still carries the notification
        log.warning("could not publish the final notification to the issue store", exc_info=True)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ralph")
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="run the issue graph to completion")
    runner.add_argument("repo", type=Path)
    runner.add_argument(
        "issue_source",
        nargs="?",
        default=None,
        help="issues directory when issue_mode is filesystem; Linear parent issue when linear",
    )
    runner.add_argument(
        "--dry-run",
        action="store_true",
        help="read the graph and print the build order. No session is opened.",
    )
    runner.add_argument(
        "--sequential",
        action="store_true",
        help="run one sub-issue at a time instead of every eligible one at once.",
    )
    runner.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        help="the harness's diagnostic verbosity: DEBUG / INFO / WARNING / ERROR.",
    )
    _add_option_flags(runner)

    checker = sub.add_parser("validate", help="refuse a run this repo is not ready for")
    checker.add_argument("repo", type=Path)
    checker.add_argument(
        "issue_source",
        nargs="?",
        default=None,
        help="issues directory when issue_mode is filesystem; Linear parent issue when linear",
    )
    checker.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        help="the harness's diagnostic verbosity: DEBUG / INFO / WARNING / ERROR.",
    )
    _add_option_flags(checker)

    args = parser.parse_args(argv)
    level = args.log_level.upper()
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    _load_env(args.repo)
    options = _options_for(
        issue_mode=args.issue_mode,
        implementer=args.implementer,
        editor=args.editor,
        integrator=args.integrator,
        protected=args.protected or DEFAULT_PROTECTED,
    )
    try:
        validate_actors(options)
    except NoActor as exc:
        print(exc)
        return 1

    if args.command == "validate":
        ready = readiness(args.repo, args.issue_source, options)
        print(render_readiness(ready))
        return 1 if ready.refusals else 0

    if args.dry_run:
        ready = readiness(args.repo, args.issue_source, options)
        if ready.refusals:
            print(render_refusals(ready.refusals))
            return 1
        print(render_plan(args.repo, args.issue_source, options))
        return 0

    print(f"options: {options.loggable()} sequential={args.sequential} log_level={level}")
    report = asyncio.run(
        run(
            args.repo,
            args.issue_source,
            options=options,
            sequential=args.sequential,
        )
    )
    print(render(report.notification, parent_issue_name(args.repo, args.issue_source, options)))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
