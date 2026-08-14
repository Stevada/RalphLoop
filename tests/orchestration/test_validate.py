"""**`ralph validate` refuses; it does not warn.**

Two halves, and they are tested differently on purpose.

The *rule* is pure — facts in, refusals out — so its tests hand it facts and never touch a
repository. That is what makes it cheap to assert the thing that actually matters: that each refusal
says something **specific**. "Your graph has a cycle" and "you are on `main`" are different
mornings, and a pre-flight that only reported *that* validation failed would have given the human
the smaller half of what it knew.

The *gathering* is real where the checked fact belongs to git or the issue source: a real git repo,
a real dirty tree, a real cyclic graph on disk. Command discovery is a port, so these tests drive it
through the fake rather than a real descriptor file.
"""

from __future__ import annotations

import shlex
from dataclasses import replace

import pytest

from ralph.cli import (
    NoActor,
    Refused,
    RunOptions,
    main,
    render_plan,
    render_refusals,
    run,
    validate,
)
from ralph.harness import (
    Check,
    Refusal,
    RepoFacts,
    build_order,
    refusals,
)
from ralph.issues import IssueGraph, SubIssue, SubIssueId, SubIssueState
from ralph.ports import CommandSource, RepoCommands
from tests.fakes import FakeCommandSource
from tests.testbed import (
    TEST_CMD,
    TargetRepo,
    make_options,
    unengaged_editor,
    unengaged_implementer,
    unengaged_integrator,
)

READY_COMMANDS = RepoCommands(test=("uv", "run", "pytest"), install=("uv", "sync"))


def validate_repo(
    repo: TargetRepo,
    options: RunOptions | None = None,
    command_source: CommandSource | None = None,
) -> tuple[Refusal, ...]:
    """`validate`, with every unattended actor supplied through the seam.

    Every test below asks a question about the *repository*. Handing in the actors takes
    `missing-actor-runtime` out of the answer, because no machine running this suite is required to
    have a Codex CLI or an SDK installed — and that check has its own tests.
    """
    return validate(
        repo.path,
        None,
        options,
        command_source,
        unengaged_implementer(),
        unengaged_editor(),
        unengaged_integrator(),
    )


@pytest.fixture
def installed_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` builds its actors from argv, so there is no seam to hand them through. The tests that
    go through it are about what the CLI prints, not about what this machine has installed."""
    monkeypatch.setattr("ralph.cli._installed", lambda runtime: True)


CLEAN = RepoFacts(
    head_branch="feature/x",
    protected=frozenset({"main", "master"}),
    dirty=(),
    has_test_command=True,
    command_error=None,
    actor_runtime_error=None,
    source_error=None,
    graph_error=None,
    pre_commit_config=None,
    pre_commit_installed=False,
)


# ── the rule ─────────────────────────────────────────────────────────────────────────────────


def test_a_clean_repo_is_not_refused() -> None:
    assert refusals(CLEAN) == ()


def test_each_refusal_says_which_morning_it_is() -> None:
    """The one property the whole pre-flight exists for. Seven different repositories, seven
    different sentences — and none of them is "validation failed"."""
    said = {
        r.check: r.reason
        for r in refusals(
            RepoFacts(
                head_branch="main",
                protected=frozenset({"main", "master"}),
                dirty=("src/app.py",),
                has_test_command=False,
                command_error=None,
                actor_runtime_error="editor 'claude': `claude_agent_sdk` is not installed",
                source_error="Linear issue 'ENG-1' was not found",
                graph_error="03-sub.md has no `## Acceptance criteria`",
                pre_commit_config=".pre-commit-config.yaml",
                pre_commit_installed=False,
            )
        )
    }

    assert set(said) == set(Check)  # all seven fire, and all seven are reported
    assert len(set(said.values())) == len(Check)  # and no two of them say the same thing

    assert "main" in said[Check.PROTECTED_BRANCH]
    assert "src/app.py" in said[Check.UNCOMMITTED_CHANGES]
    assert "pre-commit install" in said[Check.UNINSTALLED_PRE_COMMIT_HOOKS]
    assert "test command" in said[Check.MISSING_TEST_COMMAND]
    assert "claude_agent_sdk" in said[Check.MISSING_ACTOR_RUNTIME]
    assert "ENG-1" in said[Check.INVALID_ISSUE_SOURCE]
    assert "no `## Acceptance criteria`" in said[Check.INVALID_ISSUE_GRAPH]


def test_the_graph_refusal_quotes_the_parser_rather_than_summarising_it() -> None:
    """A cycle and a missing acceptance criterion arrive through the same check, and they must not
    arrive as the same sentence. The parser already said exactly what was wrong; the pre-flight's
    job is to carry that, not to paraphrase it into "the graph is bad"."""
    cycle = "01-sub.md, 02-sub.md: the graph has a cycle"
    (refused,) = refusals(replace(CLEAN, graph_error=cycle))

    assert refused.check is Check.INVALID_ISSUE_GRAPH
    assert refused.reason == cycle


def test_an_unreadable_source_and_a_malformed_graph_are_different_mornings() -> None:
    """The two ways reading the work can fail must not collapse into one check. An unreachable
    Linear is the human's infra to fix; a cyclic graph is the Planner's cut to fix — a refusal that
    called them both `graph` would send the human to the wrong file."""
    unreachable = replace(CLEAN, source_error="Linear issue 'ENG-1' was not found")
    (refused,) = refusals(unreachable)
    assert refused.check is Check.INVALID_ISSUE_SOURCE
    assert refused.reason == "Linear issue 'ENG-1' was not found"

    malformed = replace(CLEAN, graph_error="01-sub.md, 02-sub.md: the graph has a cycle")
    (refused,) = refusals(malformed)
    assert refused.check is Check.INVALID_ISSUE_GRAPH


def test_a_repo_without_pre_commit_is_not_refused_for_not_having_it() -> None:
    """Most repos do not use pre-commit, and that is a fact, not a failure. The refusal is for the
    repo that *asks* for the hook and did not install it — where every commit an Implementer makes
    quietly skips the checks the repo believes it enforces."""
    assert refusals(replace(CLEAN, pre_commit_config=None, pre_commit_installed=False)) == ()

    (refused,) = refusals(replace(CLEAN, pre_commit_config=".pre-commit-config.yaml"))
    assert refused.check is Check.UNINSTALLED_PRE_COMMIT_HOOKS


def test_the_dirty_list_is_elided_rather_than_unrolled() -> None:
    """Listing the paths is to remind someone what they forgot to commit. A hundred lines of
    `node_modules` does not do that."""
    (refused,) = refusals(replace(CLEAN, dirty=tuple(f"f{n}.py" for n in range(12))))

    assert "f0.py" in refused.reason
    assert "and 7 more" in refused.reason
    assert "f11.py" not in refused.reason


# ── the gathering ────────────────────────────────────────────────────────────────────────────


def test_a_protected_branch_is_refused_on_a_real_repo(repo: TargetRepo) -> None:
    repo.git("checkout", "main")

    (refused,) = validate_repo(repo)

    assert refused.check is Check.PROTECTED_BRANCH
    assert "'main'" in refused.reason


def test_the_protected_list_is_the_humans_to_set(repo: TargetRepo) -> None:
    options = make_options(protected=frozenset({"integration", "trunk"}))

    (refused,) = validate_repo(repo, options=options)

    assert refused.check is Check.PROTECTED_BRANCH  # `integration` is the fixture's HEAD, and now protected


def test_a_dirty_tree_is_refused_on_a_real_repo(repo: TargetRepo) -> None:
    (repo.path / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n")

    (refused,) = validate_repo(repo)

    assert refused.check is Check.UNCOMMITTED_CHANGES
    assert "calculator.py" in refused.reason


def test_preflight_consults_the_command_source(
    repo: TargetRepo, command_source: FakeCommandSource
) -> None:
    assert validate_repo(repo) == ()

    assert command_source.calls == [repo.path.resolve()]


def test_a_repo_without_a_discoverable_test_command_is_refused(repo: TargetRepo) -> None:
    missing = FakeCommandSource(RepoCommands(test=(), install=None))

    (refused,) = validate_repo(repo, command_source=missing)

    assert missing.calls == [repo.path.resolve()]
    assert refused.check is Check.MISSING_TEST_COMMAND
    assert "test command" in refused.reason


def test_the_harnesss_own_run_log_does_not_count_as_dirt(repo: TargetRepo) -> None:
    """Untracked files are not dirt. The harness writes `.scratch/run.jsonl` into the repo *while
    the run is in flight*, and a pre-flight that refused its own run log would refuse every second
    run."""
    (repo.path / ".scratch" / "run.jsonl").write_text('{"kind": "session-started"}\n')

    assert validate_repo(repo) == ()


def test_a_cyclic_graph_is_refused_on_a_real_repo(repo: TargetRepo) -> None:
    repo.write_graph({"01": ["02"], "02": ["01"]})

    (refused,) = validate_repo(repo)

    assert refused.check is Check.INVALID_ISSUE_GRAPH
    assert "cycle" in refused.reason


def test_an_incoherent_issue_source_is_refused_on_a_real_repo(repo: TargetRepo) -> None:
    """`invalid-issue-source`, not `invalid-issue-graph`: the harness never got as far as reading a
    graph. Under `issue_mode: linear`, omitting the Linear parent is an invocation problem, not a
    graph problem."""
    options = make_options(issue_mode="linear", linear_api_key="lin_x")
    (refused,) = validate_repo(repo, options=options)

    assert refused.check is Check.INVALID_ISSUE_SOURCE
    assert "issue_source" in refused.reason


def test_a_sub_issue_with_no_acceptance_criteria_is_refused(repo: TargetRepo) -> None:
    """An unattended agent has nothing else to aim at. A spec with no acceptance criteria does not
    fail the run — it produces a session that cannot be judged, which is worse."""
    (repo.issues_dir / "01-first.md").write_text("# 01 — first\n\nStatus: ready\n\nDo the thing.\n")
    repo.git("commit", "-am", "drop the criteria")

    (refused,) = validate_repo(repo)

    assert refused.check is Check.INVALID_ISSUE_GRAPH
    assert "Acceptance criteria" in refused.reason


def test_an_actor_whose_runtime_is_not_installed_is_refused(
    repo: TargetRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal that would otherwise arrive as a hung Editor session, hours in, after a sub-issue
    has already failed and a wave of tokens has already been spent."""
    monkeypatch.setattr("ralph.cli._installed", lambda runtime: runtime.name != "claude_agent_sdk")

    (refused,) = validate(
        repo.path, None, make_options(editor="claude"), None, unengaged_implementer(), None
    )

    assert refused.check is Check.MISSING_ACTOR_RUNTIME
    assert "editor 'claude'" in refused.reason
    assert "uv sync --extra editor" in refused.reason  # and it says how to fix it


def test_an_actor_handed_in_is_not_checked_for_a_runtime_it_will_not_use(
    repo: TargetRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is constructed for a role that was given an actor, so what the options *name* for
    that role is not a fact about this run — and the machine's install state is beside the point."""
    monkeypatch.setattr("ralph.cli._installed", lambda runtime: False)

    assert validate_repo(repo) == ()


def test_a_role_switched_off_is_not_checked_for_a_runtime_it_will_not_use(
    repo: TargetRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--editor none` with no Claude SDK on the machine is a fine run: nothing will be constructed
    for a role nobody fills. Refusing it would make the off switch unreachable on exactly the
    machine that needs it."""
    monkeypatch.setattr("ralph.cli._installed", lambda runtime: runtime.name != "claude_agent_sdk")

    assert validate(repo.path, options=make_options(editor="none")) == ()


def test_the_fixture_repo_is_ready_to_run(repo: TargetRepo) -> None:
    """The guard that stops every test above from passing vacuously: the same seven checks, against
    the repo they are all built on, say nothing at all."""
    assert validate_repo(repo) == ()
    assert render_refusals(()) == "ready to run."


def test_validate_prints_the_discovered_commands(
    repo: TargetRepo, capsys: pytest.CaptureFixture[str], installed_runtimes: None
) -> None:
    assert main(["validate", str(repo.path)]) == 0

    out = capsys.readouterr().out
    assert "ready to run." in out
    assert f"test: {shlex.join(TEST_CMD)}" in out
    assert "install:" not in out


def test_validate_rejects_an_editor_the_harness_does_not_know(repo: TargetRepo) -> None:
    """The harness will not invent an actor. (`none` is not this case — it is a real choice, and
    means the role is unfilled.)"""
    with pytest.raises(NoActor, match="Known: claude, codex, copilot, none"):
        validate(repo.path, options=make_options(editor="gemini"))


# ── and the run runs them too ────────────────────────────────────────────────────────────────


async def test_the_run_refuses_what_validate_refuses(repo: TargetRepo) -> None:
    """A check that only fires when a human remembers to ask for it is a check the run does not
    have. `ralph run` on `main` would fast-forward `main`. The refusal fires before an Implementer
    is ever built, so none is injected here."""
    repo.git("checkout", "main")

    with pytest.raises(Refused, match="protected"):
        await run(repo.path, None)

    assert not repo.branch_exists("ralph/01")  # zero agents started
    assert not (repo.path / ".scratch" / "run.jsonl").exists()


async def test_the_run_refuses_a_repo_without_a_discoverable_test_command(
    repo: TargetRepo,
) -> None:
    missing = FakeCommandSource(RepoCommands(test=(), install=None))

    with pytest.raises(Refused, match="test command"):
        await run(repo.path, None, command_source=missing)

    assert missing.calls == [repo.path.resolve()]
    assert not repo.branch_exists("ralph/01")
    assert not (repo.path / ".scratch" / "run.jsonl").exists()


# ── the dry run ──────────────────────────────────────────────────────────────────────────────


def test_the_dry_run_reports_the_build_order(repo: TargetRepo) -> None:
    repo.write_graph({"01": [], "02": ["01"], "03": ["01"], "04": ["02", "03"]})

    plan = render_plan(repo.path, None)

    assert "4 sub-issues, 4 edges, no cycle." in plan
    assert "wave 1: 01" in plan
    assert "wave 2: 02, 03" in plan
    assert "wave 3: 04" in plan


def test_the_dry_run_accepts_an_explicit_filesystem_issue_source(repo: TargetRepo) -> None:
    repo.write_graph({"01": [], "02": ["01"]})

    plan = render_plan(repo.path, str(repo.issues_dir))

    assert "2 sub-issues, 1 edges, no cycle." in plan
    assert "wave 1: 01" in plan
    assert "wave 2: 02" in plan


def test_the_dry_run_opens_no_session_and_touches_no_branch(
    repo: TargetRepo, capsys: pytest.CaptureFixture[str], installed_runtimes: None
) -> None:
    """It costs nothing to run, which is the whole reason it is worth having."""
    before = repo.head("integration")

    assert main(["run", "--dry-run", str(repo.path)]) == 0

    assert "sub-issues" in capsys.readouterr().out
    assert repo.head("integration") == before
    assert not repo.branch_exists("ralph/01")
    assert not (repo.path / ".worktrees").exists()


def test_the_dry_run_refuses_without_a_discoverable_test_command(
    repo: TargetRepo,
    command_source: FakeCommandSource,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command_source.commands = RepoCommands(test=(), install=None)

    assert main(["run", "--dry-run", str(repo.path)]) == 1

    out = capsys.readouterr().out
    assert "missing-test-command" in out
    assert "sub-issues" not in out
    assert not repo.branch_exists("ralph/01")


def test_the_build_order_is_derived_from_the_rule_the_scheduler_asks() -> None:
    """A dry run whose plan is not the run's plan is worse than no dry run: it is a second opinion
    about the graph, free to disagree with the one the scheduler acts on. `build_order` gets its
    waves by asking `eligible` the same question, repeatedly — so it cannot disagree.

    A sub-issue that is not `ready` is absent from the plan, not silently promoted into it: that is
    the same rule, honestly applied to a graph that is half-finished.
    """
    graph = IssueGraph(
        sub_issues={
            SubIssueId(id): SubIssue(
                id=SubIssueId(id), blocked_by=frozenset(map(SubIssueId, blockers))
            )
            for id, blockers in {"01": (), "02": ("01",), "03": ("01",)}.items()
        }
    )
    landed = {
        SubIssueId("01"): SubIssueState.LANDED,
        SubIssueId("02"): SubIssueState.READY,
        SubIssueId("03"): SubIssueState.NEEDS_HUMAN,
    }

    assert build_order(graph, landed) == ((SubIssueId("02"),),)
