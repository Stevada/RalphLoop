"""The suite runner, the installer, and the run log.

Three small adapters, and one shared theme: each of them has a failure mode whose *silent* version
would be catastrophic. The suite command and install command are CLI-owned — the harness detects
nothing — so an install that failed must not be shrugged at, and a log line that cannot be parsed
must not be skipped.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ralph.adapters.suite import InstallFailed, SubprocessTestRunner, install_once
from ralph.harness import Actor, Outcome
from ralph.issues import SubIssueId, SubIssueState
from ralph.runlog import EventKind, JsonlRunLog, RunLogError, event
from tests.testbed import TEST_CMD, TargetRepo

# --- running ------------------------------------------------------------------------------------


async def test_the_runner_reports_a_real_green_suite(repo: TargetRepo) -> None:
    runner = SubprocessTestRunner(cmd=TEST_CMD)

    result = await runner.run(repo.path)

    assert result.green is True
    assert result.duration_s > 0


async def test_the_runner_reports_a_real_red_suite_with_its_output(repo: TargetRepo) -> None:
    (repo.path / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a * b\n")
    runner = SubprocessTestRunner(cmd=TEST_CMD)

    result = await runner.run(repo.path)

    assert result.green is False
    assert "test_add" in result.output


# --- installing ---------------------------------------------------------------------------------


async def test_a_failed_install_aborts_the_run_loudly(tmp_path: Path) -> None:
    """Never `|| true`. This is the failure that, unclassified, has an Opus Editor diagnosing
    `npm ci` three times before anyone is paged."""
    with pytest.raises(InstallFailed, match="exited 1"):
        await install_once(tmp_path, ("false",))


async def test_a_successful_install_really_runs_the_command(tmp_path: Path) -> None:
    """That it runs *once per run*, in the base checkout and never per worktree, is a property of
    the scheduler — asserted end to end in `test_run_end_to_end.py`."""
    ledger = tmp_path / "installs"
    script = f"open({str(ledger)!r}, 'a').write('x')"

    await install_once(tmp_path, (sys.executable, "-c", script))

    assert ledger.read_text() == "x"


async def test_the_target_repos_command_never_sees_ralphs_own_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`VIRTUAL_ENV` and Ralph's own interpreter's `bin/` are Ralph's business, not the target
    repo's, whatever language its own install command is written in — while an unrelated variable,
    standing in for `.env` inheritance, still gets through."""
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    monkeypatch.setenv("RALPH_TEST_MARKER", "present")
    ledger = tmp_path / "env.json"
    script = f"import json, os; json.dump(dict(os.environ), open({str(ledger)!r}, 'w'))"

    await install_once(tmp_path, (sys.executable, "-c", script))

    seen = json.loads(ledger.read_text())
    assert "VIRTUAL_ENV" not in seen
    assert str(Path(sys.prefix) / "bin") not in seen.get("PATH", "").split(os.pathsep)
    assert seen.get("RALPH_TEST_MARKER") == "present"


# --- the run log --------------------------------------------------------------------------------


async def test_the_run_log_is_append_only_jsonl(tmp_path: Path) -> None:
    log = JsonlRunLog(path=tmp_path / "run.jsonl")

    await log.write(
        event(
            SubIssueId("01"),
            Actor.IMPLEMENTER,
            EventKind.SESSION_STARTED,
            SubIssueState.IN_PROGRESS,
        )
    )
    first = (tmp_path / "run.jsonl").read_text()
    await log.write(
        event(SubIssueId("01"), Actor.IMPLEMENTER, EventKind.SESSION_FINISHED, Outcome.SUCCESS)
    )
    second = (tmp_path / "run.jsonl").read_text()

    assert second.startswith(first)  # the second write did not rewrite the first line
    assert len(second.splitlines()) == 2


async def test_the_run_log_reads_back_as_typed_events_in_order(tmp_path: Path) -> None:
    log = JsonlRunLog(path=tmp_path / "run.jsonl")
    await log.write(
        event(SubIssueId("01"), Actor.IMPLEMENTER, EventKind.SESSION_FINISHED, Outcome.IMPASSE)
    )
    await log.write(
        event(SubIssueId("02"), Actor.IMPLEMENTER, EventKind.SUB_ISSUE_CLOSED, SubIssueState.LANDED)
    )

    events = JsonlRunLog(path=tmp_path / "run.jsonl").events()

    assert [e.kind for e in events] == [
        EventKind.SESSION_FINISHED,
        EventKind.SUB_ISSUE_CLOSED,
    ]
    assert events[0].details is Outcome.IMPASSE
    assert events[1].details is SubIssueState.LANDED
    assert events[0].ts <= events[1].ts


def test_an_event_kind_rejects_the_wrong_details_type() -> None:
    with pytest.raises(ValueError, match="session-finished details must be Outcome"):
        event(
            SubIssueId("01"),
            Actor.IMPLEMENTER,
            EventKind.SESSION_FINISHED,
            SubIssueState.IN_PROGRESS,
        )


def test_the_run_log_reads_legacy_payload_events(tmp_path: Path) -> None:
    (tmp_path / "run.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-07-15T00:00:00+00:00",
                "sub_issue": "01",
                "actor": "implementer",
                "kind": "session-closed",
                "payload": "impasse",
            }
        )
        + "\n"
    )

    e = JsonlRunLog(path=tmp_path / "run.jsonl").events()[0]

    assert e.kind is EventKind.SESSION_FINISHED
    assert e.details is Outcome.IMPASSE


async def test_the_run_log_carries_no_token_spend_no_diffstat_and_no_test_output(
    tmp_path: Path,
) -> None:
    """Two kinds of thing and nothing else. A reader who has to skim past a 40-line pytest dump to
    find the next event is not being told a story — those belong in the failure report, which has a
    different reader."""
    log = JsonlRunLog(path=tmp_path / "run.jsonl")
    await log.write(
        event(SubIssueId("01"), Actor.IMPLEMENTER, EventKind.SESSION_FINISHED, Outcome.SUCCESS)
    )

    line = (tmp_path / "run.jsonl").read_text()

    assert set(json.loads(line)) == {"ts", "sub_issue", "actor", "kind", "details"}


def test_an_unparseable_line_is_loud_never_skipped(tmp_path: Path) -> None:
    """A log that quietly drops what it cannot parse lies by omission — and it lies about exactly
    the run that went wrong."""
    (tmp_path / "run.jsonl").write_text('{"kind": "session-finished"}\n')

    with pytest.raises(RunLogError, match="is not an event"):
        JsonlRunLog(path=tmp_path / "run.jsonl").events()


def test_an_absent_run_log_has_no_events(tmp_path: Path) -> None:
    assert JsonlRunLog(path=tmp_path / "nope.jsonl").events() == ()
