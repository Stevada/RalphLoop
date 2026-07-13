"""The real Editor: what it is allowed to do, what it must return, and how it is stopped.

No test here calls the Anthropic API. The SDK sits behind one seam — `OpenSession` — and everything
this ticket actually builds is on this side of it: the permit, the prompt, the verdict, both bounds.
A test that needed Opus to prove a mutating tool call is denied would be testing Opus's obedience,
which is precisely the thing the harness refuses to rely on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

import pytest

from ralph.adapters.claude_editor import Ask, ClaudeCodeEditor
from ralph.adapters.editor import (
    VERDICT_CLOSE,
    VERDICT_OPEN,
    EditorSession,
    Turn,
    parse_verdict,
    read_only,
)
from ralph.domain import (
    Brief,
    EditorVerdict,
    Findings,
    Outcome,
    SessionTelemetry,
    Verdict,
    classify_editor,
    failure_report,
)
from ralph.ports import Budget, Worktree
from tests.builders import impasse, observation, suite, telemetry

SUITE: Sequence[str] = ("python", "-m", "pytest", "-q")
BRIEF = Brief(body="# 01 — make it add\n\n## Acceptance criteria\n\n- [ ] `add(1, 2) == 3`")
FINDINGS = Findings(body="")
WORKTREE = Worktree(path=Path("/w/01"), branch="ralph/01", base="integration")
SMART_ZONE = Budget(max_context_tokens=120_000, wall_clock_s=10.0)

FAILURE = failure_report(
    Outcome.IMPASSE,
    telemetry(commits=0, impasse_report=impasse()),
    suite(green=True),
)


def verdict_json(verdict: str, **fields: str) -> str:
    import json

    return f"{VERDICT_OPEN}{json.dumps({'verdict': verdict, 'rationale': 'because', **fields})}{VERDICT_CLOSE}"


# ── what the Editor may do. THE ENFORCEMENT SURFACE. ─────────────────────────────────────────


def allowed(tool: str, **input: str) -> bool:
    return read_only(tool, input, SUITE).allowed


@pytest.mark.parametrize("tool", ["Write", "Edit", "NotebookEdit", "MultiEdit", "SomeToolAddedIn2027"])
def test_every_tool_that_could_write_is_denied(tool: str) -> None:
    """Including the one that does not exist yet. The allowlist denies by **absence**, so a tool the
    SDK gains next week is denied on the day it ships rather than on the day someone remembers.

    *The moment the Editor commits, it is an Implementer with a different name.*
    """
    assert not allowed(tool, file_path="/w/01/calculator.py", content="whatever")


def test_the_editor_may_read_and_grep_and_glob() -> None:
    assert allowed("Read", file_path="/w/01/calculator.py")
    assert allowed("Grep", pattern="def add")
    assert allowed("Glob", pattern="**/*.py")


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'fixed it'",
        "git cherry-pick abc123",
        "git apply /tmp/patch",
        "git checkout -- calculator.py",
        "git reset --hard HEAD~1",
        "git push origin ralph/01",
        "git stash",
    ],
)
def test_no_git_subcommand_that_writes_is_permitted(command: str) -> None:
    """`git` is one word away from being an Implementer. The read-only subcommands are allowlisted;
    every other one — including any git grows later — is denied by absence."""
    assert not allowed("Bash", command=command)


@pytest.mark.parametrize(
    "command",
    [
        "git diff integration..HEAD",
        "git log --oneline -20",
        "git show HEAD",
        "git status",
        "git grep -n 'def add'",
        "rg 'def add' --type py",
        "cat calculator.py",
        "ls -la",
        "git log --oneline | head -5",  # a pipe only chains; both halves are checked
    ],
)
def test_the_editor_can_investigate(command: str) -> None:
    """**Reproduction, not inference.** Since declaring an impasse is cheap, the Editor's ability to
    check the Implementer's story against the repository is the only thing standing between us and a
    system where declaring an impasse always works. A permit that made investigation hard would
    quietly turn the Editor back into a very expensive way of believing the transcript."""
    assert allowed("Bash", command=command)


def test_the_editor_may_re_run_the_suite() -> None:
    """The single most valuable read-only command available to it, and the reason the permit is
    given the repo's own test command rather than a guess at one."""
    assert allowed("Bash", command="python -m pytest -q")
    assert allowed("Bash", command="python -m pytest -q test_calculator.py")


def test_it_may_not_run_python_for_anything_else() -> None:
    """`python -m pytest` is allowed because it *is* the suite. `python -c` is a file-write with
    extra steps, and it is exactly what a model reaches for when a permit blocks its first idea."""
    assert not allowed("Bash", command="python -c \"open('x','w').write('hi')\"")


@pytest.mark.parametrize(
    "command",
    [
        "git log > evidence.txt",
        "cat calculator.py >> notes.md",
        "echo $(rm -rf .)",
        "cat `whoami`",
        "rm -rf . &",
    ],
    ids=["redirect", "append", "substitution", "backtick", "background"],
)
def test_redirection_and_substitution_are_denied(command: str) -> None:
    """The subtle one. `git log > evidence.txt` passes any check that asks "is this a read-only
    command?" — because it *is* one. The write is in the shell, not the program, and it destroys the
    failed worktree a human was about to read."""
    assert not allowed("Bash", command=command)


@pytest.mark.parametrize(
    "command",
    [
        "cat calculator.py | tee copy.py",
        "git log | sed -i 's/x/y/' calculator.py",
        "git diff && git commit -m 'while I am here'",
    ],
    ids=["pipe into a writer", "pipe into an in-place edit", "and-chain"],
)
def test_a_chain_is_only_as_permitted_as_its_worst_link(command: str) -> None:
    """**Every** command in the chain is checked, not just the head. `cat calculator.py` is as
    read-only as a command gets, and `cat calculator.py | tee copy.py` writes a file — the sentence
    starts innocent and ends as an Implementer.
    """
    assert not allowed("Bash", command=command)


def test_a_denial_tells_the_model_why() -> None:
    """The reason goes back to the model, so it tries something legal rather than concluding the
    repository is broken and returning `inconclusive`."""
    denied = read_only("Bash", {"command": "git commit -m x"}, SUITE)

    assert not denied.allowed
    assert "read-only" in denied.reason.lower()


# ── what the Editor must return ──────────────────────────────────────────────────────────────


def test_a_revise_carries_the_brief_to_restart_against() -> None:
    v = parse_verdict(verdict_json("revise", revised_brief="# 01 — use add() from calculator.py"))

    assert v is not None
    assert v.verdict is Verdict.REVISE
    assert v.revision[0].body.startswith("# 01")


def test_a_revision_may_add_findings_without_touching_the_brief() -> None:
    v = parse_verdict(
        verdict_json("revise", revised_brief="# 01", revised_findings="`add()` is in calculator.py")
    )

    assert v is not None
    brief, findings = v.revision
    assert findings is not None
    assert "calculator.py" in findings.body


def test_omitted_findings_mean_leave_them_alone() -> None:
    v = parse_verdict(verdict_json("revise", revised_brief="# 01"))

    assert v is not None
    assert v.revision[1] is None


@pytest.mark.parametrize("terminal", ["planning-defect", "inconclusive"])
def test_the_terminal_verdicts_carry_no_brief(terminal: str) -> None:
    v = parse_verdict(verdict_json(terminal))

    assert v is not None
    assert v.verdict.is_terminal
    assert v.revised_brief is None


def test_a_session_that_answered_nothing_returns_no_verdict() -> None:
    """Not a bug — `infra-failed`, and the honest classification of an Editor that was asked a
    question and did not answer it. The adapter must never invent one to fill the hole: a fabricated
    `inconclusive` reads, downstream, exactly like a considered one."""
    assert parse_verdict("I had a long think about this and then wandered off") is None
    assert classify_editor(telemetry(), None) is Outcome.INFRA_FAILED


def test_a_verdict_outside_the_three_is_not_a_verdict() -> None:
    from ralph.adapters.editor import VerdictParseError

    with pytest.raises(VerdictParseError):
        parse_verdict(verdict_json("looks-fine-to-me"))


# ── how the Editor is bounded ────────────────────────────────────────────────────────────────


class StubSession:
    """A scripted Editor session. Emits its turns, then ends — unless it is killed first."""

    def __init__(self, turns: Sequence[Turn], *, pause: float = 0.0) -> None:
        self._scripted = turns
        self._pause = pause
        self.returncode: int | None = None
        self.killed = False

    async def turns(self) -> AsyncGenerator[Turn, None]:
        for turn in self._scripted:
            if self.killed:
                return  # a kill ENDS the stream. It does not raise.
            yield turn
            if self._pause:
                await asyncio.sleep(self._pause)
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else -1


async def adjudicate(
    session: EditorSession, *, must_be_terminal: bool = False, budget: Budget = SMART_ZONE
) -> tuple[SessionTelemetry, EditorVerdict | None]:
    editor = ClaudeCodeEditor(open_session=lambda ask: session, suite=SUITE)
    return await editor.adjudicate(BRIEF, FINDINGS, FAILURE, WORKTREE, budget, must_be_terminal)


async def test_a_garbled_verdict_is_no_verdict_and_is_never_quietly_repaired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The most dangerous shape a bug in this file could take.

    An unreadable verdict is tempting to "repair" into an `inconclusive` — it is terminal, it pages
    a human, it feels safe. It is not. Downstream, a fabricated `inconclusive` is **indistinguishable
    from a considered one**: the human is told the Editor looked at this and could not decide, when
    in truth the Editor may have said `planning-defect` in words the parser fumbled. The harness
    would be putting an opinion in the Editor's mouth.

    `None` is the honest answer, and it classifies `infra-failed` — a harness problem, which is
    exactly what a verdict the harness cannot read *is*.
    """
    caplog.set_level("ERROR", logger="ralph.adapters.editor")
    garbled = StubSession([f"{VERDICT_OPEN}{{'verdict': not even json,,}}{VERDICT_CLOSE}"])

    t, v = await adjudicate(garbled)

    assert v is None
    assert classify_editor(t, v) is Outcome.INFRA_FAILED
    assert "cannot read" in caplog.text  # loud, and not swallowed


async def test_one_garbled_verdict_does_not_take_the_run_down_with_it() -> None:
    """It is logged, not raised. An unreadable *impasse* is fatal — it would be misclassified as
    `silent-red` and routed to an Editor to adjudicate a failure that never happened. An unreadable
    *verdict* already lands where a garbled verdict belongs: a human, with the worktree preserved.
    Killing twenty healthy sub-issues to make the point would be a worse trade."""
    _, v = await adjudicate(StubSession([f"{VERDICT_OPEN}nonsense{VERDICT_CLOSE}"]))

    assert v is None  # no exception escaped


async def test_the_editor_returns_its_verdict_and_the_harnesss_facts() -> None:
    session = StubSession(
        [
            "Looking at the worktree.",
            observation(30_000, consumed=31_000),
            verdict_json("revise", revised_brief="# 01 — use the API that exists"),
        ]
    )

    t, v = await adjudicate(session)

    assert v is not None
    assert t.peak_context_tokens == 30_000
    assert t.commits == 0  # by construction: it was denied every tool that could make one
    assert t.diffstat == ""
    assert t.impasse_report is None  # an Editor cannot declare an impasse. It adjudicates them.
    assert classify_editor(t, v) is Outcome.SUCCESS


async def test_an_editor_that_leaves_the_smart_zone_is_killed_like_any_other_actor() -> None:
    """The Editor is bounded exactly like an Implementer — same Budget, same ceiling, same
    `run_bounded`. An Editor reasoning over 200k is a worse adjudicator than the same model over
    100k, and it is the one actor whose judgment no suite can check."""
    never_answers = StubSession(
        [observation(60_000), observation(130_000), "still thinking...", verdict_json("revise")],
        pause=0.02,
    )

    t, v = await adjudicate(never_answers)

    assert t.killed == "ceiling"
    assert v is None  # it never got to answer
    assert classify_editor(t, v) is Outcome.CEILING_EXCEEDED
    assert never_answers.killed


async def test_a_ceiling_killed_editor_pages_a_human_and_does_not_route_back() -> None:
    from ralph.domain import Actor, Destination, route

    assert route(Actor.EDITOR, Outcome.CEILING_EXCEEDED) is Destination.HUMAN


# ── and the two facts the prompt must carry ──────────────────────────────────────────────────


async def test_the_final_cycle_is_surfaced_in_the_prompt() -> None:
    """Told, not trusted. The adapter *tells* the model it is out of road; the **scheduler** refuses
    a `revise` that comes back anyway. One rule, one home — an adapter that also rejected it would
    be a second home for the cycle cap, and one day the two would disagree."""
    asks: list[Ask] = []

    def capture(ask: Ask) -> EditorSession:
        asks.append(ask)
        return StubSession([verdict_json("inconclusive")])

    editor = ClaudeCodeEditor(open_session=capture, suite=SUITE)
    await editor.adjudicate(BRIEF, FINDINGS, FAILURE, WORKTREE, SMART_ZONE, True)

    assert "final cycle" in asks[0].prompt.lower()
    assert "The harness will refuse it" in asks[0].prompt


async def test_the_adapter_does_not_itself_reject_a_final_cycle_revise() -> None:
    """It returns exactly what the Editor said. Refusing it here would put the cycle cap in two
    places, and the scheduler — which is the only thing that knows how many cycles were spent — is
    the one that owns it."""
    defiant = StubSession([verdict_json("revise", revised_brief="# 01 — one more time")])

    _, v = await adjudicate(defiant, must_be_terminal=True)

    assert v is not None  # the adapter passes it up. The scheduler is what refuses it.


async def test_the_editor_is_pointed_at_the_failed_worktree() -> None:
    """Its working directory is the wreckage, exactly as the Implementer left it. That is what makes
    reproduction possible at all."""
    asks: list[Ask] = []

    def capture(ask: Ask) -> EditorSession:
        asks.append(ask)
        return StubSession([verdict_json("inconclusive")])

    await ClaudeCodeEditor(open_session=capture, suite=SUITE).adjudicate(
        BRIEF, FINDINGS, FAILURE, WORKTREE, SMART_ZONE, False
    )

    assert asks[0].cwd == WORKTREE.path
    assert asks[0].permit("Bash", {"command": "git commit -m x"}).allowed is False


async def test_the_prompt_hands_over_the_claim_and_the_facts_to_check_it_against() -> None:
    asks: list[Ask] = []

    def capture(ask: Ask) -> EditorSession:
        asks.append(ask)
        return StubSession([verdict_json("inconclusive")])

    await ClaudeCodeEditor(open_session=capture, suite=SUITE).adjudicate(
        BRIEF, FINDINGS, FAILURE, WORKTREE, SMART_ZONE, False
    )
    prompt = asks[0].prompt

    assert "add(1, 2) == 3" in prompt  # the brief
    assert FAILURE.claim is not None
    assert FAILURE.claim.unsatisfiable_criterion in prompt  # what it claims
    assert "commits: 0" in prompt  # what the harness saw
    assert "Reproduce. Do not infer." in prompt  # and what to do about the gap


async def test_the_run_of_a_session_is_metered_even_when_it_answers() -> None:
    session = StubSession([observation(50_000, consumed=55_000), verdict_json("planning-defect")])

    t, v = await adjudicate(session)

    assert t.peak_context_tokens == 50_000
    assert t.consumed_tokens == 55_000
    assert v is not None
    assert v.verdict is Verdict.PLANNING_DEFECT
