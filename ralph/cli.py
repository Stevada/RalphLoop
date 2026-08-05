"""The composition root. **The only module that names a concrete adapter.**

Nothing downstream of here knows whether the Implementer is Codex, Copilot, or a scripted stand-in
that does not think at all — which is exactly why the stand-in can prove the whole system works
with no model in the loop.

An Implementer, at this layer, is an **argv**. Codex and Copilot are chosen here from CLI arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from ralph.adapters.claude import claude_editor
from ralph.adapters.codex import codex_editor, codex_implementer
from ralph.adapters.commands import DescriptorCommandError, DescriptorCommandSource
from ralph.adapters.copilot import copilot_editor, copilot_implementer
from ralph.adapters.git import GitCli, run_git
from ralph.adapters.suite import SubprocessTestRunner, install_once
from ralph.harness import (
    CycleLedger,
    FailureReport,
    Refusal,
    RepoFacts,
    TokenConsumption,
    build_order,
    refusals,
)
from ralph.issues import GraphError, IssueGraph, SubIssueId, SubIssueState
from ralph.issues.filesystem import FilesystemIssueStore, IssueParseError
from ralph.issues.linear import (
    LinearApiError,
    LinearGraphQLClient,
    LinearIssueStore,
    LinearIssueStoreError,
    LinearStateMap,
)
from ralph.issues.store import IssueStore
from ralph.mergequeue import MergeQueue
from ralph.notification import Notification
from ralph.ports import Budget, CommandSource, Editor, Implementer, RepoCommands, RunLog
from ralph.runlog import Event, JsonlRunLog
from ralph.scheduler import RunReport, Scheduler

log = logging.getLogger("ralph")

CODEX = "codex"
CLAUDE = "claude"
COPILOT = "copilot"


@dataclass(frozen=True, slots=True)
class ActorRuntime:
    """What a concrete adapter needs on the machine before any of its sessions can open."""

    kind: Literal["program", "module"]
    name: str
    """A CLI to find on PATH, or an SDK to import."""

    fix: str


ACTOR_RUNTIME: Mapping[str, ActorRuntime] = {
    CODEX: ActorRuntime("program", "codex", "install the Codex CLI and put `codex` on PATH"),
    CLAUDE: ActorRuntime("module", "claude_agent_sdk", "run `uv sync --extra editor`"),
    COPILOT: ActorRuntime("module", "copilot", "run `uv sync --extra copilot`"),
}
"""Here rather than in the adapters, because which actor is backed by which adapter is this module's
secret — and the pre-flight has to answer the question without constructing either one."""

FILESYSTEM = "filesystem"
LINEAR = "linear"

DEFAULT_ISSUE_MODE = FILESYSTEM
DEFAULT_IMPLEMENTER = CODEX
DEFAULT_EDITOR = CLAUDE
DEFAULT_PROTECTED = ("main", "master")

LINEAR_API_KEY = "LINEAR_API_KEY"
ENV_FILE = ".env"
PRE_COMMIT_CONFIGS = (".pre-commit-config.yaml", ".pre-commit-config.yml")

# Operational verbosity is a command-line flag: the harness's own diagnostic
# log, separate from the run log. `WARNING` keeps a clean run quiet.
DEFAULT_LOG_LEVEL = "WARNING"


class NoActor(RuntimeError):
    """The CLI named an Implementer or Editor the harness does not know. The harness will not
    invent one."""


class Refused(RuntimeError):
    """The pre-flight refused the run. Raised, not printed — `ralph run` does the same checks
    `ralph validate` does, and a check that only fires when a human remembers to ask for it is a
    check the run does not have."""


class IssueSourceError(ValueError):
    """The CLI was not given a coherent issue source."""


@dataclass(frozen=True, slots=True)
class Readiness:
    """The pre-flight result plus the commands it discovered, when discovery succeeded."""

    refusals: tuple[Refusal, ...]
    commands: RepoCommands | None


@dataclass(frozen=True, slots=True)
class RunOptions:
    """The run's resolved CLI options and one secret."""

    issue_mode: str = DEFAULT_ISSUE_MODE
    implementer: str = DEFAULT_IMPLEMENTER
    editor: str = DEFAULT_EDITOR
    protected: frozenset[str] = frozenset(DEFAULT_PROTECTED)
    linear_api_key: str | None = None

    def loggable(self) -> str:
        return (
            f"issue_mode={self.issue_mode} "
            f"implementer={self.implementer} "
            f"editor={self.editor} "
            f"protected={{{', '.join(sorted(self.protected))}}} "
            f"linear_api_key={'set' if self.linear_api_key else 'unset'}"
        )


def _options_for(
    env: Mapping[str, str] = os.environ,
    *,
    issue_mode: str = DEFAULT_ISSUE_MODE,
    implementer: str = DEFAULT_IMPLEMENTER,
    editor: str = DEFAULT_EDITOR,
    protected: Sequence[str] = DEFAULT_PROTECTED,
) -> RunOptions:
    return RunOptions(
        issue_mode=issue_mode,
        implementer=implementer,
        editor=editor,
        protected=frozenset(protected),
        linear_api_key=env.get(LINEAR_API_KEY),
    )


def _add_option_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--issue-mode",
        choices=(FILESYSTEM, LINEAR),
        default=DEFAULT_ISSUE_MODE,
        help=f"how to interpret issue_source. Defaults to {DEFAULT_ISSUE_MODE}.",
    )
    parser.add_argument(
        "--implementer",
        choices=(CODEX, COPILOT),
        default=DEFAULT_IMPLEMENTER,
        help=f"the CLI that writes code. Defaults to {DEFAULT_IMPLEMENTER}.",
    )
    parser.add_argument(
        "--editor",
        choices=(CLAUDE, CODEX, COPILOT),
        default=DEFAULT_EDITOR,
        help=f"the CLI that diagnoses failures. Defaults to {DEFAULT_EDITOR}.",
    )
    parser.add_argument(
        "--protected",
        action="append",
        default=None,
        metavar="BRANCH",
        help="branch a run refuses to start from. Repeat for more. Defaults to main and master.",
    )


def validate_actors(options: RunOptions) -> None:
    """The CLI must name actors this harness knows, without constructing their adapters."""
    if options.implementer not in {CODEX, COPILOT}:
        raise NoActor(
            f"implementer: {options.implementer!r} names no Implementer. Known: {CODEX}, {COPILOT}."
        )
    if options.editor not in {CLAUDE, CODEX, COPILOT}:
        raise NoActor(
            f"editor: {options.editor!r} names no Editor. Known: {CLAUDE}, {CODEX}, {COPILOT}."
        )


def _installed(runtime: ActorRuntime) -> bool:
    if runtime.kind == "program":
        return shutil.which(runtime.name) is not None
    # `find_spec`, not an import: asking whether the SDK is *there* must not run a line of it.
    return importlib.util.find_spec(runtime.name) is not None


def actor_runtime_error(
    options: RunOptions,
    implementer: Implementer | None = None,
    editor: Editor | None = None,
) -> str | None:
    """Every actor this run would *construct* whose runtime is missing, or `None` if there is none.

    Without this the failure surfaces at the first session that needs the runtime — which, for an
    Editor, is after a sub-issue has already failed and a wave of tokens has already been spent.

    A role handed a ready-made actor is skipped: nothing will be constructed for it, so what the
    options *name* for that role is not a fact about this run.
    """
    named_roles = [
        ("implementer", options.implementer, implementer),
        ("editor", options.editor, editor),
    ]
    missing = [
        f"{role} {named!r}: `{runtime.name}` is not installed — {runtime.fix}"
        for role, named, provided in named_roles
        if provided is None and not _installed(runtime := ACTOR_RUNTIME[named])
    ]
    return "; ".join(missing) if missing else None


def editor_of(options: RunOptions, suite: tuple[str, ...]) -> Editor:
    """Which model adjudicates a failed session.

    The Editor and the Implementer should not be the same model on the same failure — an Editor
    adjudicating an impasse declared by *itself* is the least independent sensor the system could
    have. Nothing here enforces that; it is why two CLIs back each role.
    """
    named = options.editor
    if named == CLAUDE:
        return claude_editor(suite=suite)
    if named == CODEX:
        return codex_editor(suite=suite)
    if named == COPILOT:
        return copilot_editor(suite=suite)
    validate_actors(options)
    raise AssertionError("validate_actors accepted an unknown Editor")


def implementer_of(options: RunOptions) -> Implementer:
    """Which model implements — the one decision only this module is allowed to make."""
    named = options.implementer
    if named == CODEX:
        return codex_implementer()
    if named == COPILOT:
        return copilot_implementer()
    validate_actors(options)
    raise AssertionError("validate_actors accepted an unknown Implementer")


def command_source_for() -> CommandSource:
    return DescriptorCommandSource()


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


def parent_issue_name(repo: Path, issue_source: str | None, options: RunOptions) -> str | None:
    """The parent issue's name, when the harness can derive one.

    Only filesystem mode has one: the `.scratch/<name>/issues/` directory Ralph discovered.
    Linear mode names itself through `issue_source` and has no directory to derive a name from.
    """
    if options.issue_mode != FILESYSTEM:
        return None
    given = Path(issue_source) if issue_source is not None else None
    return find_issues_dir(repo, given).parent.name


def issue_store(repo: Path, issue_source: str | None, options: RunOptions) -> IssueStore:
    """The store `options.issue_mode` names. `LinearStateMap()` is unconfigured on purpose: the four
    Linear state names are Ralph's canonical sub-issue states, hardcoded, not a per-run argument."""
    if options.issue_mode == FILESYSTEM:
        return FilesystemIssueStore(
            issues_dir=find_issues_dir(repo, Path(issue_source) if issue_source is not None else None)
        )
    if options.issue_mode == LINEAR:
        if issue_source is None:
            raise IssueSourceError(f"issue_mode: {LINEAR} needs an issue_source")
        if not options.linear_api_key:
            raise IssueSourceError(f"set {LINEAR_API_KEY} to use issue_mode: {LINEAR}")
        return LinearIssueStore(
            parent_identifier=issue_source,
            client=LinearGraphQLClient(api_key=options.linear_api_key),
            states=LinearStateMap(),
        )
    raise IssueSourceError(
        f"issue_mode: {options.issue_mode!r} is not {FILESYSTEM} or {LINEAR}."
    )


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
            actor_runtime_error=actor_runtime_error(options, implementer, editor),
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
) -> tuple[Refusal, ...]:
    return readiness(repo, issue_source, options, command_source, implementer, editor).refusals


def readiness(
    repo: Path,
    issue_source: str | None = None,
    options: RunOptions | None = None,
    command_source: CommandSource | None = None,
    implementer: Implementer | None = None,
    editor: Editor | None = None,
) -> Readiness:
    """An actor passed here is one this run will not construct, so its runtime is not checked."""
    options = options or _options_for()
    validate_actors(options)
    facts, commands = _facts_and_commands(
        repo.resolve(), issue_source, options, command_source, implementer, editor
    )
    return Readiness(
        refusals=refusals(facts),
        commands=commands if commands is not None and commands.test else None,
    )


def render_refusals(found: tuple[Refusal, ...]) -> str:
    if not found:
        return "ready to run."
    lines = [f"refusing to run ({len(found)}):"]
    lines += [f"\n  {r.check.value}\n    {r.reason}" for r in found]
    return "\n".join(lines)


def render_readiness(result: Readiness) -> str:
    if result.refusals:
        return render_refusals(result.refusals)
    lines = [
        "ready to run.",
        "commands:",
        f"  test: {shlex.join(result.commands.test if result.commands is not None else ())}",
    ]
    if result.commands is not None and result.commands.install is not None:
        lines.append(f"  install: {shlex.join(result.commands.install)}")
    return "\n".join(lines)


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
    options: RunOptions | None = None,
    command_source: CommandSource | None = None,
) -> RunReport:
    """Explicit `implementer`/`editor` override the ones the resolved options name — those are the seams
    the tests inject the scripted stand-in and a stub Editor through, and the reason no test in the
    suite calls a model."""
    repo = repo.resolve()
    options = options or _options_for()
    validate_actors(options)

    # The same checks `ralph validate` runs, and they are not advisory. A run that starts on `main`
    # has already done the damage by the time anybody reads the warning it printed.
    ready = readiness(repo, issue_source, options, command_source, implementer, editor)
    if ready.refusals:
        raise Refused(render_refusals(ready.refusals))
    if ready.commands is None:
        raise AssertionError("readiness accepted a run without a test command")
    commands = ready.commands

    git = GitCli(repo=repo)
    integration = git.head_branch()

    # Install runs once in the base checkout, before anything is dispatched.
    runner = SubprocessTestRunner(cmd=commands.test)
    if commands.install is not None:
        await install_once(repo, commands.install)

    store = issue_store(repo, issue_source, options)
    selected_implementer = implementer if implementer is not None else implementer_of(options)
    selected_editor = editor if editor is not None else editor_of(options, commands.test)
    parent = parent_issue_name(repo, issue_source, options)
    scratch = repo / ".scratch" / parent if parent is not None else repo / ".scratch"
    scheduler = Scheduler(
        repo=repo,
        git=git,
        store=store,
        run_log=NarratedRunLog(JsonlRunLog(path=scratch / "run.jsonl")),
        implementer=selected_implementer,
        editor=selected_editor,
        merge_queue=MergeQueue(git=git, runner=runner, integration=integration),
        integration=integration,
        budget=budget or Budget(),
        parent_issue_name=parent,
    )
    report = await scheduler.run()
    notification = render(report.notification, parent)
    try:
        await store.publish_notification(notification)
    except Exception:  # noqa: BLE001 — stdout still carries the notification
        log.warning("could not publish the final notification to the issue store", exc_info=True)
    return report


def render_event(e: Event) -> str:
    """One run-log event, for a human watching it happen.

    The same five fields the JSONL line carries, in the same order — this is a *view* of the record,
    not a second record. UTC like the file, and time-of-day only: a run is hours, not days, and the
    date would be five characters of noise on every line.
    """
    return (
        f"{e.ts:%H:%M:%S}  {e.sub_issue:<10} {e.actor.value:<12} "
        f"{e.kind.value:<17} {e.details.value}"
    )


@dataclass(frozen=True, slots=True)
class NarratedRunLog:
    """A `RunLog` that also narrates to the terminal as it writes.

    The file is the record; this is the only account of a run *while it is still happening*. The
    notification comes at the end, which is hours too late to tell you a wave has started, and
    `tail -f` on a path the run computes for itself is a poor substitute for the run saying so.

    Ordered file-first on purpose: the terminal must never claim something the record does not.
    """

    inner: RunLog

    async def write(self, e: Event) -> None:
        await self.inner.write(e)
        # Unbuffered, because the whole value here is timeliness — a narration that arrives in a
        # 4KB block when the run ends is the notification again, with worse formatting.
        print(render_event(e), flush=True)

    def events(self) -> tuple[Event, ...]:
        return self.inner.events()


def render_consumption(c: TokenConsumption) -> str:
    """The total, and the breakdown where there is one.

    A vendor that reported only a total gets to have said only that. Printing three zeros beside a
    real total would read as a session that generated nothing, which is a different claim entirely.
    """
    if c.input_tokens is None or c.cache_read_tokens is None or c.output_tokens is None:
        return f"{c.consumed_tokens} tokens (no breakdown)"
    return (
        f"{c.consumed_tokens} tokens "
        f"({c.input_tokens} in, {c.cache_read_tokens} cached, {c.output_tokens} out)"
    )


def render(n: Notification, parent_issue_name: str | None = None) -> str:
    """**One** notification, at the end.

    The bar: *if you cannot tell from this alone whether to spend your first ten minutes reading a
    diff or rewriting a PRD, it has failed.* So each escalation leads with what kind of failure it
    was, says what it is holding up, and — where the model left one — quotes the criterion it
    believes it cannot satisfy. Most urgent first; there is no scrolling to find the important one.
    """
    lines = [f"landed: {', '.join(n.landed) if n.landed else 'nothing'}"]
    if n.consumption:
        lines.append("\nconsumption:")
        for c in n.consumption:
            lines.append(
                f"  {c.sub_issue}: {render_consumption(c.consumption)}, "
                f"{c.auto_compactions} auto-compactions"
            )
        lines.append(
            f"  total: {render_consumption(n.total_consumption)}, "
            f"{n.total_auto_compactions} auto-compactions"
        )
    if not n.escalations:
        return "\n".join(lines)

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
        worktree_dir = (
            f".worktrees/failed/{parent_issue_name}/{e.sub_issue}"
            if parent_issue_name is not None
            else f".worktrees/failed/{e.sub_issue}"
        )
        lines.append(
            f"    harness: {commits} commit{'' if commits == 1 else 's'}, "
            f"suite {_suite_summary(e.report)}, "
            f"worktree preserved at {worktree_dir}"
        )
    return "\n".join(lines)


def _suite_summary(report: FailureReport) -> str:
    if report.suite is None:
        return "not run"
    return "green" if report.suite.green else "red"


def _load_env(repo: Path) -> None:
    """Load `<repo>/.env` into the environment — the one secret Ralph reads (`LINEAR_API_KEY`) and
    the target repo's own variables alike, for the subprocesses that inherit it. A real export still
    wins: the file is the default, the ambient environment the override. Nothing here is policed by
    name; the one secret is absent-checked where it is used, only on an `issue_mode: linear` run."""
    env_file = repo / ENV_FILE
    if env_file.exists():
        load_dotenv(env_file)


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

    print(f"options: {options.loggable()} log_level={level}")
    report = asyncio.run(
        run(
            args.repo,
            args.issue_source,
            options=options,
        )
    )
    print(render(report.notification, parent_issue_name(args.repo, args.issue_source, options)))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
