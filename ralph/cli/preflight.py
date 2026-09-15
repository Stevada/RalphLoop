"""The pre-flight, and the two commands that report it without opening a session.

Whether a run may start is `harness/rules/preflight.py`'s to decide; gathering the facts it decides
over means touching the repo, so it happens here. `validate` prints every refusal and `render_plan`
prints the build order — neither spends a token.
"""

from __future__ import annotations

from pathlib import Path

from ralph.adapters.commands import DescriptorCommandError
from ralph.adapters.git import GitCli, run_git
from ralph.cli.actors import (
    IssueSourceError,
    actor_runtime_error,
    command_source_for,
    issue_store,
    validate_actors,
)
from ralph.cli.options import RunOptions, _options_for
from ralph.cli.presentation import Readiness
from ralph.harness import Refusal, RepoFacts, build_order, refusals
from ralph.issues import GraphError, IssueGraph, SubIssueId, SubIssueState
from ralph.issues.filesystem import IssueParseError
from ralph.issues.linear import LinearApiError, LinearIssueStoreError
from ralph.ports import CommandSource, Editor, Implementer, Integrator, RepoCommands

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
