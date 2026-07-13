"""The suite runner, the installer, and the run log.

Three small adapters, and one shared theme: each of them has a failure mode whose *silent* version
would be catastrophic. A suite that cannot be found must not be green. An install that failed must
not be shrugged at. A log line that cannot be parsed must not be skipped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ralph.adapters.runlog import JsonlRunLog, RunLogError
from ralph.adapters.suite import (
    InstallFailed,
    NoSuiteFound,
    SubprocessTestRunner,
    detect_install_cmd,
    detect_test_cmd,
    install_once,
)
from ralph.domain import Outcome, SubIssueId, SubIssueState
from ralph.events import event
from tests.testbed import TargetRepo

# --- detection ----------------------------------------------------------------------------------


def test_npm_is_detected(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    assert detect_test_cmd(tmp_path) == ("npm", "test")


def test_pytest_is_detected(tmp_path: Path) -> None:
    (tmp_path / "test_thing.py").write_text("def test_x() -> None: ...\n")
    assert detect_test_cmd(tmp_path)[-3:] == ("-m", "pytest", "-q")


def test_make_is_detected(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\t@echo ok\n")
    assert detect_test_cmd(tmp_path) == ("make", "test")


def test_a_makefile_with_no_test_target_is_not_a_suite(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("build:\n\t@echo ok\n")
    with pytest.raises(NoSuiteFound):
        detect_test_cmd(tmp_path)


def test_the_override_beats_every_detector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setenv("RALPH_TEST_CMD", "cargo test --all")

    assert detect_test_cmd(tmp_path) == ("cargo", "test", "--all")


def test_a_repo_with_no_suite_and_no_override_is_a_loud_fatal_error(tmp_path: Path) -> None:
    """Never a green `SuiteResult`. A repo whose tests the harness cannot run would make every
    classification downstream a lie — and `silent-red` would become unreachable."""
    with pytest.raises(NoSuiteFound, match="no test suite detected"):
        detect_test_cmd(tmp_path)


# --- running ------------------------------------------------------------------------------------


async def test_the_runner_reports_a_real_green_suite(repo: TargetRepo) -> None:
    runner = SubprocessTestRunner(cmd=detect_test_cmd(repo.path))

    result = await runner.run(repo.path)

    assert result.green is True
    assert result.duration_s > 0


async def test_the_runner_reports_a_real_red_suite_with_its_output(repo: TargetRepo) -> None:
    (repo.path / "calculator.py").write_text("def add(a: int, b: int) -> int:\n    return a * b\n")
    runner = SubprocessTestRunner(cmd=detect_test_cmd(repo.path))

    result = await runner.run(repo.path)

    assert result.green is False
    assert "test_add" in result.output


# --- installing ---------------------------------------------------------------------------------


def test_nothing_to_install_is_a_fact_not_a_failure(tmp_path: Path) -> None:
    assert detect_install_cmd(tmp_path) is None


async def test_a_failed_install_aborts_the_run_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never `|| true`. This is the failure that, unclassified, has an Opus Editor diagnosing
    `npm ci` three times before anyone is paged."""
    monkeypatch.setenv("RALPH_INSTALL_CMD", "false")

    with pytest.raises(InstallFailed, match="exited 1"):
        await install_once(tmp_path)


async def test_a_successful_install_really_runs_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """That it runs *once per run*, in the base checkout and never per worktree, is a property of
    the scheduler — asserted end to end in `test_run_end_to_end.py`."""
    ledger = tmp_path / "installs"
    script = f"open({str(ledger)!r}, 'a').write('x')"
    monkeypatch.setenv("RALPH_INSTALL_CMD", f"{sys.executable} -c {script!r}")

    await install_once(tmp_path)

    assert ledger.read_text() == "x"


# --- the run log --------------------------------------------------------------------------------


async def test_the_run_log_is_append_only_jsonl(tmp_path: Path) -> None:
    log = JsonlRunLog(path=tmp_path / "run.jsonl")

    await log.write(event(SubIssueId("01"), "session-opened", SubIssueState.IN_PROGRESS))
    first = (tmp_path / "run.jsonl").read_text()
    await log.write(event(SubIssueId("01"), "session-closed", Outcome.SUCCESS))
    second = (tmp_path / "run.jsonl").read_text()

    assert second.startswith(first)  # the second write did not rewrite the first line
    assert len(second.splitlines()) == 2


async def test_the_run_log_reads_back_as_typed_events_in_order(tmp_path: Path) -> None:
    log = JsonlRunLog(path=tmp_path / "run.jsonl")
    await log.write(event(SubIssueId("01"), "session-closed", Outcome.SILENT_RED))
    await log.write(event(SubIssueId("02"), "terminal", SubIssueState.LANDED))

    events = JsonlRunLog(path=tmp_path / "run.jsonl").events()

    assert [e.kind for e in events] == ["session-closed", "terminal"]
    assert events[0].payload is Outcome.SILENT_RED
    assert events[1].payload is SubIssueState.LANDED
    assert events[0].ts <= events[1].ts


async def test_the_run_log_carries_no_token_spend_no_diffstat_and_no_test_output(
    tmp_path: Path,
) -> None:
    """Two kinds of thing and nothing else. A reader who has to skim past a 40-line pytest dump to
    find the next event is not being told a story — those belong in the failure report, which has a
    different reader."""
    log = JsonlRunLog(path=tmp_path / "run.jsonl")
    await log.write(event(SubIssueId("01"), "session-closed", Outcome.SUCCESS))

    line = (tmp_path / "run.jsonl").read_text()

    assert set(json.loads(line)) == {"ts", "sub_issue", "kind", "payload"}


def test_an_unparseable_line_is_loud_never_skipped(tmp_path: Path) -> None:
    """A log that quietly drops what it cannot parse lies by omission — and it lies about exactly
    the run that went wrong."""
    (tmp_path / "run.jsonl").write_text('{"kind": "session-closed"}\n')

    with pytest.raises(RunLogError, match="is not an event"):
        JsonlRunLog(path=tmp_path / "run.jsonl").events()


def test_an_absent_run_log_has_no_events(tmp_path: Path) -> None:
    assert JsonlRunLog(path=tmp_path / "nope.jsonl").events() == ()
