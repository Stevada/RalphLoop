"""The transcript store: one file per session, at a name no two sessions share, under a footer.

Two claims here. The *key*, because a store that put every session of a sub-issue in one file would
still look like it worked — right up to the run where three actors and three cycles all had
something to say, and eight of the nine were gone. And the *footer*, because a conclusion filed
away from the observations behind it is a conclusion nobody can check.
"""

from __future__ import annotations

from pathlib import Path

from ralph.harness import Actor, Outcome, TokenConsumption
from ralph.issues import SubIssueId
from ralph.ports import Budget, FinishedSession
from ralph.transcripts import FileTranscripts
from tests.builders import DEFAULT_CONSUMPTION, telemetry

ID = SubIssueId("01")
BUDGET = Budget(wall_clock_s=1800.0)


def finished(
    *,
    sub_issue: SubIssueId = ID,
    cycle: int = 1,
    actor: Actor = Actor.IMPLEMENTER,
    outcome: Outcome = Outcome.SUCCESS,
    said: str = "",
    merge_finished: bool | None = None,
    exit_code: int = 0,
    commits: int = 1,
    wall_clock_s: float = 60.0,
    diffstat: str = " 1 file changed, 1 insertion(+)",
    consumption: TokenConsumption = DEFAULT_CONSUMPTION,
) -> FinishedSession:
    return FinishedSession(
        sub_issue=sub_issue,
        cycle=cycle,
        actor=actor,
        outcome=outcome,
        telemetry=telemetry(
            transcript=said,
            exit_code=exit_code,
            commits=commits,
            wall_clock_s=wall_clock_s,
            diffstat=diffstat,
            consumption=consumption,
        ),
        budget=BUDGET,
        merge_finished=merge_finished,
    )


def body_of(path: Path) -> str:
    """The session's half of the file, with the harness's footer taken back off."""
    return path.read_text().split("\nralph| ")[0]


async def test_a_transcript_lands_under_its_sub_issue_cycle_and_actor(tmp_path: Path) -> None:
    store = FileTranscripts(root=tmp_path)

    await store.write(finished(cycle=2, actor=Actor.EDITOR, said="the spec asks for two things"))

    assert body_of(tmp_path / "01" / "2-editor.log") == "the spec asks for two things"


async def test_the_directory_is_made_on_the_way(tmp_path: Path) -> None:
    """The scratch directory exists before a run; `transcripts/<sub-issue>/` does not."""
    store = FileTranscripts(root=tmp_path / "nowhere" / "transcripts")

    await store.write(finished(said="done"))

    assert (tmp_path / "nowhere" / "transcripts" / "01" / "1-implementer.log").exists()


async def test_a_silent_session_still_leaves_a_file(tmp_path: Path) -> None:
    """An empty file says the session said nothing; an absent one would say it never ran.

    The footer is still there — it is the harness's account, not the session's, and the harness has
    something to say about a session precisely when the session did not.
    """
    store = FileTranscripts(root=tmp_path)

    await store.write(finished(said=""))

    written = (tmp_path / "01" / "1-implementer.log").read_text()
    assert body_of(tmp_path / "01" / "1-implementer.log") == ""
    assert "outcome:        success" in written


async def test_no_two_sessions_of_one_run_share_a_name(tmp_path: Path) -> None:
    """Every session a sub-issue can produce, at once: three cycles times three actors.

    This is the whole justification for the key. Nine sessions, nine files, nine distinct bodies —
    and each read back individually, because nine files with the same contents would also count to
    nine.
    """
    store = FileTranscripts(root=tmp_path)
    sessions = [(cycle, actor) for cycle in (1, 2, 3) for actor in Actor]

    for cycle, actor in sessions:
        await store.write(finished(cycle=cycle, actor=actor, said=f"{actor.value} on cycle {cycle}"))

    written = sorted((tmp_path / "01").iterdir())
    assert len(written) == len(sessions)
    for cycle, actor in sessions:
        path = tmp_path / "01" / f"{cycle}-{actor.value}.log"
        assert body_of(path) == f"{actor.value} on cycle {cycle}"


async def test_two_sub_issues_do_not_share_a_directory(tmp_path: Path) -> None:
    store = FileTranscripts(root=tmp_path)

    await store.write(finished(said="mine"))
    await store.write(finished(sub_issue=SubIssueId("02"), said="also mine"))

    assert body_of(tmp_path / "01" / "1-implementer.log") == "mine"
    assert body_of(tmp_path / "02" / "1-implementer.log") == "also mine"


# ── the footer ───────────────────────────────────────────────────────────────────────────────


async def test_the_footer_puts_the_conclusion_beside_what_it_was_drawn_from(tmp_path: Path) -> None:
    """The failure that motivated the footer, reproduced.

    A real reconciliation exited 0 after three minutes of a thirty-minute budget, committed
    nothing, and was classified `success` — because the only thing asked about the work was whether
    git still had a merge open, and it did not. Every one of those facts was known to the harness
    and none of them was written down, so answering "why did it say success?" meant reading the
    classifier's source. Here they are all on one page, and the answer takes a glance.
    """
    store = FileTranscripts(root=tmp_path)

    await store.write(
        finished(
            actor=Actor.INTEGRATOR,
            outcome=Outcome.SUCCESS,
            said="I have staged the resolution.\n",
            merge_finished=True,
            commits=0,
            wall_clock_s=187.0,
            exit_code=0,
            diffstat="",
        )
    )

    written = (tmp_path / "01" / "1-integrator.log").read_text()
    assert "I have staged the resolution." in written
    assert "ralph| outcome:        success" in written
    assert "ralph| commits:        0" in written
    assert "ralph| merge finished: yes" in written
    assert "ralph| wall clock:     187.0s of 1800s" in written
    assert "ralph| diffstat:       nothing" in written


async def test_the_gates_observation_is_absent_for_the_actors_nobody_asks_it_about(
    tmp_path: Path,
) -> None:
    """`merge_finished` is the Integrator's classification input alone. Printing it as `no` for an
    Implementer would report an observation the harness never made."""
    store = FileTranscripts(root=tmp_path)

    await store.write(finished(actor=Actor.IMPLEMENTER, outcome=Outcome.IMPASSE, commits=0))

    assert "merge finished" not in (tmp_path / "01" / "1-implementer.log").read_text()


async def test_every_footer_of_a_run_lines_up_at_the_same_column(tmp_path: Path) -> None:
    """The Integrator's footer carries a label none of the others does, so a column measured over
    the labels actually printed would be two characters narrower for the other two actors. One run
    would then produce two formats, and a reader diffing two transcripts would see every line
    differ."""
    store = FileTranscripts(root=tmp_path)

    await store.write(finished(actor=Actor.IMPLEMENTER))
    await store.write(finished(actor=Actor.INTEGRATOR, merge_finished=False))

    for actor in (Actor.IMPLEMENTER, Actor.INTEGRATOR):
        written = (tmp_path / "01" / f"1-{actor.value}.log").read_text()
        assert "ralph| outcome:        success" in written


async def test_a_bare_total_is_not_dressed_up_as_a_breakdown(tmp_path: Path) -> None:
    """A vendor that reports only a total gets a total. Printing `0 in, 0 cached, 0 out` beside it
    would read as a measurement of three things nobody counted."""
    store = FileTranscripts(root=tmp_path)

    await store.write(finished(consumption=TokenConsumption.total_only(4_242)))

    assert "ralph| consumption:    4,242 tokens\n" in (
        tmp_path / "01" / "1-implementer.log"
    ).read_text()
