"""**`ralph run` works.**

The tracer bullet. Every test here goes through `ralph.cli.run` — the real CLI, the real
filesystem store, real git, real worktrees, the real merge gate, a real suite, and a real agent
subprocess. Failure cases inject a scripted Editor so the test does not call a real model.

The only thing that is not real is the agent's intelligence, and that is the one thing the harness
was never trusting anyway.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ralph.adapters.git import GitCli
from ralph.cli import run
from ralph.harness import Outcome, Verdict
from ralph.issues import SubIssueId, SubIssueState
from ralph.ports import Budget, RepoCommands
from tests.builders import telemetry, verdict as editor_verdict
from tests.fakes import FakeCommandSource, FakeEditor
from tests.testbed import (
    Behaviour,
    PARENT_ISSUE_NAME,
    StandInAgent,
    TEST_CMD,
    TargetRepo,
    make_options,
    stand_in_implementer as stand_in,
    unengaged_editor,
)


def terminal_editor() -> FakeEditor:
    return FakeEditor(
        scripted=[(telemetry(commits=0), editor_verdict(Verdict.PLANNING_DEFECT))]
    )


async def test_ralph_run_lands_a_sub_issue_end_to_end(
    repo: TargetRepo, agent: StandInAgent, command_source: FakeCommandSource
) -> None:
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    # Both sub-issues in the fixture's graph land, and 02 could only start once 01 had.
    assert report.landed == (SubIssueId("01"), SubIssueId("02"))
    assert report.clean
    assert command_source.calls == [repo.path.resolve()]

    # The work is on the integration branch.
    assert (repo.path / "feature_01.py").exists()
    assert (repo.path / "feature_02.py").exists()
    assert repo.commit_count("integration") == 3

    # The history is linear, and the suite is green on it.
    assert repo.git("log", "--merges", "--oneline", "integration") == ""
    assert repo.run_suite() is True

    # `Status: landed` — written after the fast-forward, by the harness, in the file.
    assert "Status: landed" in (repo.issues_dir / "01-first.md").read_text()
    assert "Status: landed" in (repo.issues_dir / "02-second.md").read_text()


async def test_the_run_log_tells_the_true_story_in_order(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    lines = [json.loads(x) for x in (repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text().splitlines()]
    story = [(e["sub_issue"], e["actor"], e["kind"], e["details"]) for e in lines]

    # Every successful session says whose it was. The Editor's own lines are added only when an
    # Implementer failure routes to adjudication, as covered in `test_editor_loop.py`.
    assert story == [
        ("01", "implementer", "session-started", "in-progress"),
        ("01", "implementer", "session-finished", "success"),
        ("01", "implementer", "sub-issue-closed", "landed"),  # after the fast-forward
        ("02", "implementer", "session-started", "in-progress"),
        ("02", "implementer", "session-finished", "success"),
        ("02", "implementer", "sub-issue-closed", "landed"),
    ]
    assert all(set(e) == {"ts", "sub_issue", "actor", "kind", "details"} for e in lines)


async def test_every_run_log_event_is_narrated_to_the_terminal_as_it_happens(
    repo: TargetRepo, agent: StandInAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file is the record, but nobody watches a file. A run is hours long and its only other
    output arrives at the end — so what reaches the terminal must be *every* event, in the order the
    record has them, and nothing the record does not have."""
    await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    log = (repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text().splitlines()
    narrated = [
        line for line in capsys.readouterr().out.splitlines() if "session-" in line or "closed" in line
    ]

    assert len(narrated) == len(log)
    for line, recorded in zip(narrated, (json.loads(x) for x in log), strict=True):
        for field in ("sub_issue", "actor", "kind", "details"):
            assert recorded[field] in line
        assert recorded["ts"][11:19] in line  # the record's own UTC clock, not a second one


async def test_a_red_base_is_caught_by_the_suite_gate(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The scheduler no longer owns a pre-session suite run. The first committed session reaches
    the merge gate, and its suite gate is what refuses the red tree."""
    (repo.path / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a * b\n")
    repo.git("add", "-A")
    repo.git("commit", "-m", "break the base")

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert report.failed == {SubIssueId("01"): Outcome.INTEGRATION_FAILED}
    assert repo.branch_exists(f"ralph/{PARENT_ISSUE_NAME}_01")
    assert repo.commit_count("integration") == 2


async def test_a_failed_install_aborts_the_run_before_a_single_agent_starts(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    commands = FakeCommandSource(RepoCommands(test=TEST_CMD, install=("false",)))

    with pytest.raises(Exception, match="exited 1"):
        await run(
            repo.path,
            None,
            implementer=stand_in(agent, Behaviour.SUCCEED),
            editor=unengaged_editor(),
            command_source=commands,
        )

    assert commands.calls == [repo.path.resolve()]
    assert not repo.branch_exists(f"ralph/{PARENT_ISSUE_NAME}_01")


async def test_the_install_runs_once_in_the_base_checkout_never_per_worktree(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """Two sub-issues, two worktrees, one install. N worktrees must not mean N installs of the
    same tree, with the first failure discovered N times."""
    ledger = repo.path.parent / "installs"
    installer = repo.path.parent / "install.py"
    installer.write_text(f"open({str(ledger)!r}, 'a').write('x')\n")
    commands = FakeCommandSource(
        RepoCommands(test=TEST_CMD, install=(sys.executable, str(installer)))
    )

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        command_source=commands,
    )

    assert len(report.landed) == 2
    assert commands.calls == [repo.path.resolve()]
    assert ledger.read_text() == "x"


async def test_a_repo_without_install_command_skips_base_install(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    ledger = repo.path.parent / "installs"
    commands = FakeCommandSource(RepoCommands(test=TEST_CMD, install=None))

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        command_source=commands,
    )

    assert len(report.landed) == 2
    assert commands.calls == [repo.path.resolve()]
    assert not ledger.exists()


async def test_the_editor_allowlist_uses_the_discovered_test_command(
    repo: TargetRepo, agent: StandInAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = FakeCommandSource(RepoCommands(test=TEST_CMD, install=None))
    seen: list[tuple[str, ...]] = []

    def editor_for(suite: tuple[str, ...]) -> FakeEditor:
        seen.append(suite)
        return terminal_editor()

    monkeypatch.setattr("ralph.cli.claude_editor", editor_for)
    # No Editor through the seam: this test is about the one the *run* builds. Which means the
    # pre-flight would check for a Claude runtime that `editor_for` has just replaced.
    monkeypatch.setattr("ralph.cli._installed", lambda runtime: True)

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        command_source=commands,
        options=make_options(editor="claude"),
    )

    assert report.clean
    assert seen == [TEST_CMD]


async def test_an_undeclared_impasse_session_does_not_land(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """It exits 0 and commits nothing. The prototype called that a skip and moved on."""
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.COMMIT_NOTHING),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert report.landed == ()
    assert report.failed == {SubIssueId("01"): Outcome.IMPASSE}
    assert repo.commit_count("integration") == 1
    assert "Status: needs-human" in (repo.issues_dir / "01-first.md").read_text()

    # 02 is blocked by 01, which never landed, so it never became eligible and never got a turn.
    # Nothing was written to say so — quarantine-and-drain needs no `skipped` state.
    assert not repo.branch_exists(f"ralph/{PARENT_ISSUE_NAME}_02")
    assert "Status: ready" in (repo.issues_dir / "02-second.md").read_text()


async def test_a_session_that_commits_a_red_suite_fails_at_the_merge_gate(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The dangerous one: it exits 0, it committed, and it is broken. The scheduler calls that a
    success; the merge gate's suite gate is what refuses it."""
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.RED_SUITE),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert report.failed == {SubIssueId("01"): Outcome.INTEGRATION_FAILED}
    assert repo.commit_count("integration") == 1
    assert repo.run_suite() is True  # integration was never touched, so it is still green


async def test_a_hanging_session_is_killed_and_does_not_land(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    report = await run(
        repo.path,
        None,
        budget=Budget(wall_clock_s=1.0),
        implementer=stand_in(agent, Behaviour.HANG),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert report.failed == {SubIssueId("01"): Outcome.INFRA_FAILED}
    assert repo.commit_count("integration") == 1


async def test_an_impasse_does_not_land_and_is_recorded(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.IMPASSE),
        editor=terminal_editor(),
        options=make_options(),
    )

    assert report.failed == {SubIssueId("01"): Outcome.IMPASSE}
    assert ("01", "session-finished", "impasse") in [
        (e["sub_issue"], e["kind"], e["details"])
        for e in (
            json.loads(x)
            for x in (repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text().splitlines()
        )
    ]


async def test_a_read_only_issue_store_does_not_crash_the_run(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The store is best-effort by contract — a run must not die because the tracker was
    unreachable. The run log is the authoritative record, and it still has the truth."""
    (repo.issues_dir / "01-first.md").chmod(0o444)
    (repo.issues_dir / "02-second.md").chmod(0o444)

    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert report.landed == (SubIssueId("01"), SubIssueId("02"))
    assert "Status: ready" in (repo.issues_dir / "01-first.md").read_text()  # the mirror failed
    assert (repo.path / "feature_01.py").exists()  # the work landed anyway

    events = json.loads(
        "[" + ",".join((repo.path / ".scratch" / PARENT_ISSUE_NAME / "run.jsonl").read_text().splitlines()) + "]"
    )
    assert sum(e["details"] == SubIssueState.LANDED.value for e in events) == 2


async def test_a_landed_sub_issue_leaves_no_worktree_and_no_branch(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """A landed sub-issue is finished, and finished work has nowhere left to live but the
    integration branch. `.worktrees/` is where a human looks for wreckage — an active checkout per
    landed sub-issue is a run that reads as if it half-failed, and the branch it sat on would
    refuse to be cut again.

    The last assertion is the one that matters: the cleanup must remove the checkout, not the work.
    """
    report = await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    assert len(report.landed) == 2
    for id in ("01", "02"):
        assert not (repo.path / ".worktrees" / "active" / PARENT_ISSUE_NAME / id).exists()
        assert not repo.branch_exists(f"ralph/{PARENT_ISSUE_NAME}_{id}")

    # The commits survived their branch: they are on integration, which is where they landed.
    assert GitCli(repo=repo.path).commits_between("main", "integration") >= 2


async def test_each_session_leaves_its_transcript_beside_the_run_log(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The run log says a session happened; the transcript says what it said.

    Asserted here rather than only against the fake because the filename is the whole design — a
    store wired to the wrong root, or handed a cycle it computed itself, fails nowhere else.
    """
    await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.SUCCEED),
        editor=unengaged_editor(),
        options=make_options(),
    )

    transcripts = repo.path / ".scratch" / PARENT_ISSUE_NAME / "transcripts"
    assert sorted(p.relative_to(transcripts) for p in transcripts.rglob("*.log")) == [
        Path("01/1-implementer.log"),
        Path("02/1-implementer.log"),
    ]
    # The `succeed` stand-in commits without narrating, so the session's own half is empty — the
    # claim that it ran and said nothing, which is not what an absent file would say. The harness's
    # half is there regardless, and on a silent session it is the only account there is.
    written = (transcripts / "01" / "1-implementer.log").read_text()
    assert written.startswith("ralph| launched: ")
    assert "ralph| outcome:        success" in written
    assert "ralph| commits:        1" in written


async def test_a_transcript_holds_what_the_session_actually_said(
    repo: TargetRepo, agent: StandInAgent
) -> None:
    """The Implementer's own words, kept whole — including the sentinel the harness parsed out of
    them. This is the artifact a human opens next to the preserved worktree."""
    await run(
        repo.path,
        None,
        implementer=stand_in(agent, Behaviour.IMPASSE),
        editor=terminal_editor(),
        options=make_options(),
    )

    body = (
        repo.path / ".scratch" / PARENT_ISSUE_NAME / "transcripts" / "01" / "1-implementer.log"
    ).read_text()
    assert "the second acceptance criterion of 01" in body
