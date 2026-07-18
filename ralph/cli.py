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
import logging
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from ralph.adapters.claude_editor import ClaudeCodeEditor, claude_sdk_session
from ralph.adapters.codex import codex_implementer
from ralph.adapters.copilot import copilot_editor, copilot_implementer
from ralph.adapters.git import GitCli, run_git
from ralph.adapters.suite import SubprocessTestRunner, install_once
from ralph.config import (
    CONFIG_FILE,
    ENV_FILE,
    LINEAR_API_KEY,
    PRE_COMMIT_CONFIGS,
    Config,
    ConfigError,
)
from ralph.harness import (
    CycleLedger,
    Refusal,
    RepoFacts,
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
from ralph.ports import Budget, Editor, Implementer
from ralph.runlog import JsonlRunLog
from ralph.scheduler import DEFAULT_CONCURRENCY, RunReport, Scheduler

log = logging.getLogger("ralph")

CODEX = "codex"
CLAUDE = "claude"
COPILOT = "copilot"

FILESYSTEM = "filesystem"
LINEAR = "linear"


class _NoEditorOverride:
    pass


_NO_EDITOR_OVERRIDE = _NoEditorOverride()

# Operational verbosity is a command-line flag, not configuration — the harness's own diagnostic
# log, separate from the run log. `WARNING` keeps a clean run quiet.
DEFAULT_LOG_LEVEL = "WARNING"


class NoAgent(RuntimeError):
    """`ralph.yaml` named an Implementer or Editor the harness does not know. The harness will not
    invent one."""


class Refused(RuntimeError):
    """The pre-flight refused the run. Raised, not printed — `ralph run` does the same checks
    `ralph validate` does, and a check that only fires when a human remembers to ask for it is a
    check the run does not have."""


class IssueSourceError(ValueError):
    """The CLI was not given a coherent issue source."""


def validate_agents(config: Config) -> None:
    """`ralph.yaml` must name actors this harness knows, without constructing their adapters."""
    if config.implementer not in {CODEX, COPILOT}:
        raise NoAgent(
            f"implementer: {config.implementer!r} in {CONFIG_FILE} names no Implementer. "
            f"Known: {CODEX}, {COPILOT}."
        )
    if config.editor not in {CLAUDE, COPILOT}:
        raise NoAgent(
            f"editor: {config.editor!r} in {CONFIG_FILE} names no Editor. "
            f"Known: {CLAUDE}, {COPILOT}."
        )


def editor_of(config: Config) -> Editor:
    """Which model adjudicates a failed session.

    The Editor and the Implementer should not be the same model on the same failure — an Editor
    adjudicating an impasse declared by *itself* is the least independent sensor the system could
    have. Nothing here enforces that; it is why two CLIs back each role.
    """
    named = config.editor
    if named == CLAUDE:
        return ClaudeCodeEditor(open_session=claude_sdk_session, suite=config.test_cmd)
    if named == COPILOT:
        # Read-only, but guaranteed by Copilot's own permission engine rather than by a function
        # this harness owns and tests. Weaker on purpose, and worth knowing here at the point of
        # choosing: see `CopilotEditor`. It buys independence — an Editor that is not the model
        # that just failed.
        return copilot_editor(suite=config.test_cmd)
    validate_agents(config)
    raise AssertionError("validate_agents accepted an unknown Editor")


def implementer_of(config: Config) -> Implementer:
    """Which model implements — the one decision only this module is allowed to make."""
    named = config.implementer
    if named == CODEX:
        return codex_implementer()
    if named == COPILOT:
        return copilot_implementer()
    validate_agents(config)
    raise AssertionError("validate_agents accepted an unknown Implementer")


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


def issue_store(repo: Path, issues: Path | None, linear: str | None, config: Config) -> IssueStore:
    """The store `config.source` names. `LinearStateMap()` is unconfigured on purpose: the four
    Linear state names are Ralph's canonical sub-issue states, hardcoded, not a per-run argument."""
    if config.source == FILESYSTEM:
        return FilesystemIssueStore(issues_dir=find_issues_dir(repo, issues))
    if config.source == LINEAR:
        if linear is None:
            raise IssueSourceError(f"source: {LINEAR} needs a parent issue — pass --linear-parent")
        if issues is not None:
            raise IssueSourceError("pass either an issues directory or --linear-parent, not both")
        if not config.linear_api_key:
            raise IssueSourceError(f"set {LINEAR_API_KEY} to use source: {LINEAR}")
        return LinearIssueStore(
            parent_identifier=linear,
            client=LinearGraphQLClient(api_key=config.linear_api_key),
            states=LinearStateMap(),
        )
    raise IssueSourceError(
        f"source: {config.source!r} in {CONFIG_FILE} is not {FILESYSTEM} or {LINEAR}."
    )


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


def _read_graph(
    repo: Path, issues: Path | None, linear: str | None, config: Config
) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]:
    return issue_store(repo, issues, linear, config).read_graph()


def facts_about(repo: Path, issues: Path | None, linear: str | None, config: Config) -> RepoFacts:
    """Ask the world the five questions, and hand the answers to a rule that cannot ask anything.

    Each `except` is narrow and each keeps the raiser's own message: `IssueParseError` already says
    exactly which file has no acceptance criteria, and `GraphError` already names the cycle. A
    pre-flight that rephrased them would be a second, worse copy of a sentence that is already right.
    """
    git = GitCli(repo=repo)
    pre_commit_config, installed = _pre_commit(repo)

    source_error: str | None = None
    graph_error: str | None = None
    try:
        _read_graph(repo, issues, linear, config)
    except (IssueParseError, GraphError, LinearIssueStoreError) as exc:
        graph_error = str(exc)
    except (FileNotFoundError, IssueSourceError, LinearApiError) as exc:
        source_error = str(exc)

    return RepoFacts(
        head_branch=git.head_branch(),
        protected=config.protected,
        dirty=git.dirty_files(),
        source_error=source_error,
        graph_error=graph_error,
        pre_commit_config=pre_commit_config,
        pre_commit_installed=installed,
    )


def validate(
    repo: Path,
    issues: Path | None = None,
    linear: str | None = None,
    config: Config | None = None,
) -> tuple[Refusal, ...]:
    config = config or Config.resolve(repo)
    validate_agents(config)
    return refusals(facts_about(repo.resolve(), issues, linear, config))


def render_refusals(found: tuple[Refusal, ...]) -> str:
    if not found:
        return "ready to run."
    lines = [f"refusing to run ({len(found)}):"]
    lines += [f"\n  {r.check.value}\n    {r.reason}" for r in found]
    return "\n".join(lines)


def render_plan(
    repo: Path,
    issues: Path | None,
    linear: str | None = None,
    config: Config | None = None,
) -> str:
    """What `--dry-run` prints: the graph as the harness reads it, and the order it would work in.

    The cheapest possible dogfood — it parses every sub-issue, resolves every edge, and proves the
    graph is acyclic, and it costs nothing to run because no session is ever opened.
    """
    config = config or Config.resolve(repo)
    validate_agents(config)
    graph, states = _read_graph(repo.resolve(), issues, linear, config)
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
    implementer: Implementer | None = None,
    editor: Editor | _NoEditorOverride = _NO_EDITOR_OVERRIDE,
    linear: str | None = None,
    config: Config | None = None,
) -> RunReport:
    """Explicit `implementer`/`editor` override the ones `ralph.yaml` names — those are the seams
    the tests inject the scripted stand-in and a stub Editor through, and the reason no test in the
    suite calls a model."""
    repo = repo.resolve()
    config = config or Config.resolve(repo)
    validate_agents(config)

    # The same checks `ralph validate` runs, and they are not advisory. A run that starts on `main`
    # has already done the damage by the time anybody reads the warning it printed.
    found = validate(repo, issues, linear, config)
    if found:
        raise Refused(render_refusals(found))

    git = GitCli(repo=repo)
    integration = git.head_branch()

    # The suite command comes from `ralph.yaml`, resolved once; install runs once in the base
    # checkout, before anything is dispatched.
    runner = SubprocessTestRunner(cmd=config.test_cmd)
    await install_once(repo, config.install_cmd)

    store = issue_store(repo, issues, linear, config)
    selected_implementer = implementer if implementer is not None else implementer_of(config)
    selected_editor = editor_of(config) if isinstance(editor, _NoEditorOverride) else editor
    scheduler = Scheduler(
        repo=repo,
        git=git,
        store=store,
        run_log=JsonlRunLog(path=repo / ".scratch" / "run.jsonl"),
        runner=runner,
        implementer=selected_implementer,
        editor=selected_editor,
        merge_queue=MergeQueue(git=git, runner=runner, integration=integration),
        integration=integration,
        budget=budget or Budget(),
        concurrency=concurrency,
    )
    report = await scheduler.run()
    notification = render(report.notification)
    try:
        await store.publish_notification(notification)
    except Exception:  # noqa: BLE001 — stdout still carries the notification
        log.warning("could not publish the final notification to the issue store", exc_info=True)
    return report


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


def _load_env(repo: Path) -> None:
    """Load `<repo>/.env` into the environment — the one secret Ralph reads (`LINEAR_API_KEY`) and
    the target repo's own variables alike, for the subprocesses that inherit it. A real export still
    wins: the file is the default, the ambient environment the override. Nothing here is policed by
    name; the one secret is absent-checked where it is used, only on a `source: linear` run."""
    env_file = repo / ENV_FILE
    if env_file.exists():
        load_dotenv(env_file)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ralph")
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="run the issue graph to completion")
    runner.add_argument("repo", type=Path)
    runner.add_argument("issues", type=Path, nargs="?", default=None)
    runner.add_argument(
        "--linear-parent",
        default=None,
        help="read sub-issues from this Linear parent issue instead of .scratch/<phase>/issues",
    )
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
    runner.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        help="the harness's diagnostic verbosity: DEBUG / INFO / WARNING / ERROR.",
    )

    checker = sub.add_parser("validate", help="refuse a run this repo is not ready for")
    checker.add_argument("repo", type=Path)
    checker.add_argument("issues", type=Path, nargs="?", default=None)
    checker.add_argument(
        "--linear-parent",
        default=None,
        help="read sub-issues from this Linear parent issue instead of .scratch/<phase>/issues",
    )
    checker.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        help="the harness's diagnostic verbosity: DEBUG / INFO / WARNING / ERROR.",
    )

    args = parser.parse_args(argv)
    level = args.log_level.upper()
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    _load_env(args.repo)
    try:
        config = Config.resolve(args.repo)
        validate_agents(config)
    except (ConfigError, NoAgent) as exc:
        print(exc)
        return 1

    if args.command == "validate":
        found = validate(args.repo, args.issues, args.linear_parent, config)
        print(render_refusals(found))
        return 1 if found else 0

    if args.dry_run:
        print(render_plan(args.repo, args.issues, args.linear_parent, config))
        return 0

    print(f"configuration: {config.loggable()} log_level={level}")
    report = asyncio.run(
        run(
            args.repo,
            args.issues,
            concurrency=args.parallel,
            linear=args.linear_parent,
            config=config,
        )
    )
    print(render(report.notification))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
