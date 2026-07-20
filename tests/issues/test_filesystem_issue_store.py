"""Parsing the issue files.

The `Status:` line is the most dangerous thing in this module. A silent default would either run a
sub-issue the Planner never authorised, or skip one it did — and both look like nothing happening.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph.issues.filesystem import FilesystemIssueStore, IssueParseError
from ralph.harness import Actor
from ralph.issues import Brief, Findings, SessionConsumption, SubIssueId, SubIssueState
from ralph.runlog import EventKind, event

READY = "Status: ready\n\n## Acceptance criteria\n\n- [ ] It works.\n"


def issue(dir: Path, name: str, body: str = READY, title: str = "a sub-issue") -> Path:
    path = dir / name
    path.write_text(f"# {title}\n\n{body}")
    return path


def test_an_issue_directory_parses_into_a_graph(tmp_path: Path) -> None:
    issue(tmp_path, "01-contract.md", title="01 — the contract")
    issue(tmp_path, "02-rules.md", READY + "\n## Blocked by\n\n- #01\n")

    graph, states = FilesystemIssueStore(issues_dir=tmp_path).read_graph()

    assert set(graph.sub_issues) == {SubIssueId("01"), SubIssueId("02")}
    assert graph.sub_issues[SubIssueId("01")].title == "01 — the contract"
    assert graph.blockers_of(SubIssueId("02")) == frozenset({SubIssueId("01")})
    assert states == {SubIssueId("01"): SubIssueState.READY, SubIssueId("02"): SubIssueState.READY}


@pytest.mark.parametrize(
    "bullet",
    ["#02", "PDA #02", "02-remove-payload.md", "#2"],
    ids=["hash", "prefixed-hash", "filename", "unpadded"],
)
def test_every_blocker_spelling_names_the_same_sub_issue(tmp_path: Path, bullet: str) -> None:
    """Matched by number, not by string. `#2` and `02` are the same sub-issue, which is what the
    human writing the file meant, and the harness should not be the one to disagree."""
    issue(tmp_path, "02-remove-payload.md")
    issue(tmp_path, "03-dependent.md", READY + f"\n## Blocked by\n\n- {bullet}\n")

    graph, _ = FilesystemIssueStore(issues_dir=tmp_path).read_graph()

    assert graph.blockers_of(SubIssueId("03")) == frozenset({SubIssueId("02")})


def test_a_none_blocker_is_no_blocker(tmp_path: Path) -> None:
    issue(tmp_path, "01-first.md", READY + "\n## Blocked by\n\n- None — can start immediately\n")

    graph, _ = FilesystemIssueStore(issues_dir=tmp_path).read_graph()

    assert graph.blockers_of(SubIssueId("01")) == frozenset()


@pytest.mark.parametrize("status", ["done", "ready-for-agent", "not-started", "blocked"])
def test_a_non_canonical_status_is_a_loud_parse_error(tmp_path: Path, status: str) -> None:
    """`done` belongs to the parent issue. `ready-for-agent` and `not-started` do not exist. None
    of them may quietly become `ready`."""
    issue(tmp_path, "01-first.md", f"Status: {status}\n\n## Acceptance criteria\n\n- [ ] x\n")

    with pytest.raises(IssueParseError, match=status):
        FilesystemIssueStore(issues_dir=tmp_path).read_graph()


def test_a_missing_status_line_is_a_loud_parse_error(tmp_path: Path) -> None:
    issue(tmp_path, "01-first.md", "## Acceptance criteria\n\n- [ ] x\n")

    with pytest.raises(IssueParseError, match="Status"):
        FilesystemIssueStore(issues_dir=tmp_path).read_graph()


def test_a_missing_acceptance_criteria_section_is_a_loud_parse_error(tmp_path: Path) -> None:
    """An unattended agent has nothing else to aim at."""
    issue(tmp_path, "01-first.md", "Status: ready\n\nBuild something nice.\n")

    with pytest.raises(IssueParseError, match="Acceptance criteria"):
        FilesystemIssueStore(issues_dir=tmp_path).read_graph()


def test_a_blocker_that_names_no_sibling_is_a_loud_parse_error(tmp_path: Path) -> None:
    issue(tmp_path, "01-first.md", READY + "\n## Blocked by\n\n- #99\n")

    with pytest.raises(IssueParseError, match="99"):
        FilesystemIssueStore(issues_dir=tmp_path).read_graph()


def test_an_empty_issue_directory_is_a_loud_error(tmp_path: Path) -> None:
    with pytest.raises(IssueParseError):
        FilesystemIssueStore(issues_dir=tmp_path).read_graph()


def test_content_hands_back_the_brief_and_the_findings(tmp_path: Path) -> None:
    issue(
        tmp_path,
        "01-first.md",
        READY + "\n## Findings\n\nThe client's retry logic swallows the expected error.\n",
    )

    brief, findings = FilesystemIssueStore(issues_dir=tmp_path).content(SubIssueId("01"))

    assert "Acceptance criteria" in brief.body
    assert brief.revision == 0
    assert findings.body == "The client's retry logic swallows the expected error."


def test_a_sub_issue_with_no_findings_has_empty_findings(tmp_path: Path) -> None:
    issue(tmp_path, "01-first.md")

    _, findings = FilesystemIssueStore(issues_dir=tmp_path).content(SubIssueId("01"))

    assert findings.body == ""


async def test_a_terminal_event_is_mirrored_into_the_status_line(tmp_path: Path) -> None:
    path = issue(tmp_path, "01-first.md")
    store = FilesystemIssueStore(issues_dir=tmp_path)

    await store.write_event(
        event(SubIssueId("01"), Actor.IMPLEMENTER, EventKind.SUB_ISSUE_CLOSED, SubIssueState.LANDED)
    )

    assert "Status: landed" in path.read_text()
    _, states = store.read_graph()
    assert states[SubIssueId("01")] is SubIssueState.LANDED


async def test_a_session_event_does_not_touch_the_status_line(tmp_path: Path) -> None:
    """The run log is the harness's record of sessions. The `Status:` line is a human's view of
    where a sub-issue got to, and it moves only on a terminal state."""
    path = issue(tmp_path, "01-first.md")
    store = FilesystemIssueStore(issues_dir=tmp_path)

    await store.write_event(
        event(
            SubIssueId("01"),
            Actor.IMPLEMENTER,
            EventKind.SESSION_STARTED,
            SubIssueState.IN_PROGRESS,
        )
    )

    assert "Status: ready" in path.read_text()


async def test_consumption_records_round_trip_for_a_sub_issue(tmp_path: Path) -> None:
    issue(tmp_path, "01-first.md")
    store = FilesystemIssueStore(issues_dir=tmp_path)

    await store.record_consumption(
        SubIssueId("01"),
        SessionConsumption(
            actor=Actor.IMPLEMENTER, consumed_tokens=123_000, auto_compactions=2
        ),
    )
    await store.record_consumption(
        SubIssueId("01"), SessionConsumption(actor=Actor.EDITOR, consumed_tokens=45_000)
    )

    assert store.consumption(SubIssueId("01")) == (
        SessionConsumption(actor=Actor.IMPLEMENTER, consumed_tokens=123_000, auto_compactions=2),
        SessionConsumption(actor=Actor.EDITOR, consumed_tokens=45_000),
    )


# --- revisions ------------------------------------------------------------------------------------


async def test_a_revision_is_stored_beside_the_planners_original_never_over_it(
    tmp_path: Path,
) -> None:
    """**Revision 0 is what a human diffs against.**

    It is the evidence for the one design bet most likely to fail: that the Editor rewrites briefs
    without softening them. Three rounds of revision quietly turning "reject the request" into "log
    a warning" is *spec drift*, and the only way to catch it is to still have the original. A harness
    that rewrote the brief in place would destroy the evidence with the very mechanism under
    suspicion.
    """
    issue(tmp_path, "01-first.md", title="01 — as the Planner wrote it")
    store = FilesystemIssueStore(issues_dir=tmp_path)
    original = (tmp_path / "01-first.md").read_bytes()

    await store.record_revision(
        SubIssueId("01"), Brief(body="rewritten once"), Findings(body="we learned a thing")
    )
    await store.record_revision(
        SubIssueId("01"), Brief(body="rewritten twice"), Findings(body="we learned another")
    )

    # Two revisions later, revision 0 is byte-for-byte what the Planner wrote.
    revisions = tmp_path / "revisions" / "01"
    assert (revisions / "0-brief.md").read_bytes() == original
    assert "as the Planner wrote it" in (revisions / "0-brief.md").read_text()

    # And every revision is kept, not just the newest — the drift is only visible as a sequence.
    assert (revisions / "1-brief.md").read_text() == "rewritten once"
    assert (revisions / "2-brief.md").read_text() == "rewritten twice"


async def test_the_next_session_reads_the_newest_revision(tmp_path: Path) -> None:
    """`content` is what the next Implementer session works from, and after a revision that is the
    Editor's brief — not the one the last session already failed against."""
    issue(tmp_path, "01-first.md")
    store = FilesystemIssueStore(issues_dir=tmp_path)

    assert store.content(SubIssueId("01"))[0].revision == 0  # the Planner's

    await store.record_revision(SubIssueId("01"), Brief(body="v1"), Findings(body="f1"))
    await store.record_revision(SubIssueId("01"), Brief(body="v2"), Findings(body="f2"))

    brief, findings = store.content(SubIssueId("01"))
    assert (brief.body, brief.revision) == ("v2", 2)
    assert findings.body == "f2"


async def test_brief_and_findings_round_trip_as_separate_fields(tmp_path: Path) -> None:
    """A revision may change one without the other. They are separate files because they are
    separate ideas: the brief is the bar, the findings are what was learned about the repo."""
    issue(tmp_path, "01-first.md")
    store = FilesystemIssueStore(issues_dir=tmp_path)

    await store.record_revision(SubIssueId("01"), Brief(body="the bar"), Findings(body="learned x"))
    await store.record_revision(SubIssueId("01"), Brief(body="the bar"), Findings(body="learned y"))

    brief, findings = store.content(SubIssueId("01"))
    assert brief.body == "the bar"  # unmoved across a revision that changed only the findings
    assert findings.body == "learned y"


async def test_a_revision_directory_is_not_mistaken_for_a_sub_issue(tmp_path: Path) -> None:
    """The `*.md` glob defines the graph. A revision landing in it would add a phantom sub-issue —
    with no `Status:` line, which is a fatal parse error, so the *next run* would die rather than
    the one that wrote it."""
    issue(tmp_path, "01-first.md")
    store = FilesystemIssueStore(issues_dir=tmp_path)

    await store.record_revision(SubIssueId("01"), Brief(body="v1"), Findings(body=""))

    graph, _ = store.read_graph()
    assert set(graph.sub_issues) == {SubIssueId("01")}
