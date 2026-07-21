"""**`ralph validate` refuses; it does not warn.**

Two halves, and they are tested differently on purpose.

The *rule* is pure — facts in, refusals out — so its tests hand it facts and never touch a
repository. That is what makes it cheap to assert the thing that actually matters: that each refusal
says something **specific**. "Your graph has a cycle" and "you are on `main`" are different
mornings, and a pre-flight that only reported *that* validation failed would have given the human
the smaller half of what it knew.

The *gathering* is real: a real git repo, a real dirty tree, a real cyclic graph on disk. A refusal
that fires on a hand-built `RepoFacts` and never on a real repository is a refusal that does not
exist.

**Zero mocks.** Nothing in this file imports `tests.fakes`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ralph.issues.filesystem import FilesystemIssueStore
from ralph.cli import NoAgent, Refused, main, render_plan, render_refusals, run, validate
from ralph.harness import (
    Check,
    RepoFacts,
    build_order,
    refusals,
)
from ralph.issues import IssueGraph, SubIssue, SubIssueId, SubIssueState
from tests.testbed import TargetRepo, make_options

BUILD_HARNESS = Path(__file__).parents[2] / ".scratch" / "build_harness" / "issues"

CLEAN = RepoFacts(
    head_branch="feature/x",
    protected=frozenset({"main", "master"}),
    dirty=(),
    source_error=None,
    graph_error=None,
    pre_commit_config=None,
    pre_commit_installed=False,
)


# ── the rule ─────────────────────────────────────────────────────────────────────────────────


def test_a_clean_repo_is_not_refused() -> None:
    assert refusals(CLEAN) == ()


def test_each_refusal_says_which_morning_it_is() -> None:
    """The one property the whole pre-flight exists for. Five different repositories, five different
    sentences — and none of them is "validation failed"."""
    said = {
        r.check: r.reason
        for r in refusals(
            RepoFacts(
                head_branch="main",
                protected=frozenset({"main", "master"}),
                dirty=("src/app.py",),
                source_error="Linear issue 'ENG-1' was not found",
                graph_error="03-sub.md has no `## Acceptance criteria`",
                pre_commit_config=".pre-commit-config.yaml",
                pre_commit_installed=False,
            )
        )
    }

    assert set(said) == set(Check)  # all five fire, and all five are reported
    assert len(set(said.values())) == 5  # and no two of them say the same thing

    assert "main" in said[Check.PROTECTED_BRANCH]
    assert "src/app.py" in said[Check.UNCOMMITTED_CHANGES]
    assert "pre-commit install" in said[Check.UNINSTALLED_PRE_COMMIT_HOOKS]
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

    (refused,) = validate(repo.path)

    assert refused.check is Check.PROTECTED_BRANCH
    assert "'main'" in refused.reason


def test_the_protected_list_is_the_humans_to_set(repo: TargetRepo) -> None:
    options = make_options(protected=frozenset({"integration", "trunk"}))

    (refused,) = validate(repo.path, options=options)

    assert refused.check is Check.PROTECTED_BRANCH  # `integration` is the fixture's HEAD, and now protected


def test_a_dirty_tree_is_refused_on_a_real_repo(repo: TargetRepo) -> None:
    (repo.path / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a - b\n")

    (refused,) = validate(repo.path)

    assert refused.check is Check.UNCOMMITTED_CHANGES
    assert "calculator.py" in refused.reason


def test_the_harnesss_own_run_log_does_not_count_as_dirt(repo: TargetRepo) -> None:
    """Untracked files are not dirt. The harness writes `.scratch/run.jsonl` into the repo *while
    the run is in flight*, and a pre-flight that refused its own run log would refuse every second
    run."""
    (repo.path / ".scratch" / "run.jsonl").write_text('{"kind": "session-started"}\n')

    assert validate(repo.path) == ()


def test_a_cyclic_graph_is_refused_on_a_real_repo(repo: TargetRepo) -> None:
    repo.write_graph({"01": ["02"], "02": ["01"]})

    (refused,) = validate(repo.path)

    assert refused.check is Check.INVALID_ISSUE_GRAPH
    assert "cycle" in refused.reason


def test_an_incoherent_issue_source_is_refused_on_a_real_repo(repo: TargetRepo) -> None:
    """`invalid-issue-source`, not `invalid-issue-graph`: the harness never got as far as reading a
    graph. Under `issue_mode: linear`, omitting the Linear parent is an invocation problem, not a
    graph problem."""
    options = make_options(issue_mode="linear", linear_api_key="lin_x")
    (refused,) = validate(repo.path, options=options)

    assert refused.check is Check.INVALID_ISSUE_SOURCE
    assert "issue_source" in refused.reason


def test_a_sub_issue_with_no_acceptance_criteria_is_refused(repo: TargetRepo) -> None:
    """An unattended agent has nothing else to aim at. A spec with no acceptance criteria does not
    fail the run — it produces a session that cannot be judged, which is worse."""
    (repo.issues_dir / "01-first.md").write_text("# 01 — first\n\nStatus: ready\n\nDo the thing.\n")
    repo.git("commit", "-am", "drop the criteria")

    (refused,) = validate(repo.path)

    assert refused.check is Check.INVALID_ISSUE_GRAPH
    assert "Acceptance criteria" in refused.reason


def test_the_fixture_repo_is_ready_to_run(repo: TargetRepo) -> None:
    """The guard that stops every test above from passing vacuously: the same five checks, against
    the repo they are all built on, say nothing at all."""
    assert validate(repo.path) == ()
    assert render_refusals(()) == "ready to run."


def test_validate_rejects_editor_none(repo: TargetRepo) -> None:
    with pytest.raises(NoAgent, match="Known: claude, codex, copilot"):
        validate(repo.path, options=make_options(editor="none"))


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
    repo: TargetRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    """It costs nothing to run, which is the whole reason it is worth having."""
    before = repo.head("integration")

    assert main(["run", "--dry-run", str(repo.path)]) == 0

    assert "sub-issues" in capsys.readouterr().out
    assert repo.head("integration") == before
    assert not repo.branch_exists("ralph/01")
    assert not (repo.path / ".worktrees").exists()


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
                id=SubIssueId(id), title=id, blocked_by=frozenset(map(SubIssueId, blockers))
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


# ── the cheapest dogfood there is ────────────────────────────────────────────────────────────


def test_the_harness_can_read_its_own_issue_graph() -> None:
    """`ralph run --dry-run` against **this repo's own** build order. Every sub-issue that built the
    harness is parsed, every edge resolved, and the graph proved acyclic — by the same code that
    would run them. It costs nothing, and it is the only test in the suite whose input is the real
    thing rather than a fixture shaped like it.
    """
    graph, _ = FilesystemIssueStore(issues_dir=BUILD_HARNESS).read_graph()

    assert len(graph.sub_issues) == 11
    assert graph.blockers_of(SubIssueId("11")) == frozenset(map(SubIssueId, ("05", "07", "10")))
    assert graph.transitively_blocked_by(SubIssueId("11")) >= frozenset(map(SubIssueId, ("01", "02")))

    # The order the harness would have built itself in, had it existed to do so. Asserted as a
    # property rather than a literal: the states on disk change as the build lands, and a test that
    # pinned today's waves would be a test of the calendar.
    order = build_order(graph, dict.fromkeys(graph.sub_issues, SubIssueState.READY))
    landed_by = {id: n for n, wave in enumerate(order) for id in wave}

    assert sorted(landed_by) == sorted(graph.sub_issues)  # every one of them is reachable
    for id, sub in graph.sub_issues.items():
        for blocker in sub.blocked_by:
            assert landed_by[blocker] < landed_by[id]
