"""The transcript store: one file per session, at a name no two sessions share.

The interesting claim is the *key*, not the write. A store that put every session of a sub-issue in
one file would still look like it worked — right up to the run where three actors and three cycles
all had something to say, and eight of the nine were gone.
"""

from __future__ import annotations

from pathlib import Path

from ralph.harness import Actor
from ralph.issues import SubIssueId
from ralph.transcripts import FileTranscripts

ID = SubIssueId("01")


async def test_a_transcript_lands_under_its_sub_issue_cycle_and_actor(tmp_path: Path) -> None:
    store = FileTranscripts(root=tmp_path)

    await store.write(ID, 2, Actor.EDITOR, "the spec asks for two things")

    assert (tmp_path / "01" / "2-editor.log").read_text() == "the spec asks for two things"


async def test_the_directory_is_made_on_the_way(tmp_path: Path) -> None:
    """The scratch directory exists before a run; `transcripts/<sub-issue>/` does not."""
    store = FileTranscripts(root=tmp_path / "nowhere" / "transcripts")

    await store.write(ID, 1, Actor.IMPLEMENTER, "done")

    assert (tmp_path / "nowhere" / "transcripts" / "01" / "1-implementer.log").exists()


async def test_a_silent_session_still_leaves_a_file(tmp_path: Path) -> None:
    """An empty file says the session said nothing; an absent one would say it never ran."""
    store = FileTranscripts(root=tmp_path)

    await store.write(ID, 1, Actor.IMPLEMENTER, "")

    assert (tmp_path / "01" / "1-implementer.log").read_text() == ""


async def test_no_two_sessions_of_one_run_share_a_name(tmp_path: Path) -> None:
    """Every session a sub-issue can produce, at once: three cycles times three actors.

    This is the whole justification for the key. Nine sessions, nine files, nine distinct bodies —
    and each read back individually, because nine files with the same contents would also count to
    nine.
    """
    store = FileTranscripts(root=tmp_path)
    sessions = [(cycle, actor) for cycle in (1, 2, 3) for actor in Actor]

    for cycle, actor in sessions:
        await store.write(ID, cycle, actor, f"{actor.value} on cycle {cycle}")

    written = sorted((tmp_path / "01").iterdir())
    assert len(written) == len(sessions)
    for cycle, actor in sessions:
        path = tmp_path / "01" / f"{cycle}-{actor.value}.log"
        assert path.read_text() == f"{actor.value} on cycle {cycle}"


async def test_two_sub_issues_do_not_share_a_directory(tmp_path: Path) -> None:
    store = FileTranscripts(root=tmp_path)

    await store.write(ID, 1, Actor.IMPLEMENTER, "mine")
    await store.write(SubIssueId("02"), 1, Actor.IMPLEMENTER, "also mine")

    assert (tmp_path / "01" / "1-implementer.log").read_text() == "mine"
    assert (tmp_path / "02" / "1-implementer.log").read_text() == "also mine"
