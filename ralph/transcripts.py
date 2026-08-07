"""The transcripts: everything each session said, one file per session.

The run log stays skimmable by carrying no bodies — it answers *what happened, in what order*. This
is where the bodies go, and the two artifacts index each other without either having to reference
the other: a `session-finished` event already names the sub-issue and the actor, and the cycle is
the only thing a reader brings to the filename.

`<sub-issue>/<cycle>-<actor>.log` is a total key by construction. One Implementer session per cycle,
one Editor session after it, and at most one Integrator dispatched by the merge gate during that
cycle's landing — so no two sessions ever compete for a name.

Nothing in the harness reads these back. They are for the human who opens a quarantined worktree and
wants to know what the session was thinking, which is the one question the notification cannot
answer and the diff can only partly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ralph.harness import TokenConsumption
from ralph.ports import FinishedSession, HARNESS_LINE


@dataclass(frozen=True, slots=True)
class FileTranscripts:
    """One file per session, under `root`."""

    root: Path

    async def write(self, session: FinishedSession) -> None:
        """Written even when the session said nothing. A session that said nothing is a fact about
        that session, and an absent file would claim instead that no session ran."""
        path = self.root / str(session.sub_issue) / f"{session.cycle}-{session.actor.value}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session.telemetry.transcript + _footer(session))


def _footer(session: FinishedSession) -> str:
    """The harness's own account of the session, under everything the session said.

    Every line here is an *input* to the classification, printed beside its result — never a
    restatement of the rule that combined them. The rules live in `harness/rules/`, and a footer
    that re-derived them would be a second copy of the taxonomy, free to drift from the one that
    actually decides. Printing the inputs is also strictly more useful: it shows the facts a
    classifier deliberately ignored, and an outcome that looks wrong beside those is exactly the
    bug worth catching.
    """
    t = session.telemetry
    # A `None` value is a fact nobody established, and it is dropped — but only after the column is
    # measured, so every footer in a run lines up at the same one. Two transcripts that indented
    # differently would read as two different formats.
    facts: list[tuple[str, str | None]] = [
        ("outcome", session.outcome.value),
        ("exit code", str(t.exit_code)),
        ("killed", t.killed or "no"),
        ("wall clock", f"{t.wall_clock_s:.1f}s of {session.budget.wall_clock_s:.0f}s"),
        ("commits", str(t.commits)),
        ("impasse", "declared" if t.impasse_report is not None else "none"),
        ("consumption", _tokens(t.consumption)),
        ("compactions", str(t.auto_compactions)),
        ("merge finished", _yes_no(session.merge_finished)),
        ("diffstat", t.diffstat.strip() or "nothing"),
    ]
    width = max(len(label) for label, _ in facts) + 2
    lines = [f"── the harness, on this {session.actor.value} session ──"]
    lines += [f"{label + ':':<{width}}{value}" for label, value in facts if value is not None]
    return "\n" + "".join(f"{HARNESS_LINE}{line}\n" for line in lines)


def _yes_no(fact: bool | None) -> str | None:
    return None if fact is None else ("yes" if fact else "no")


def _tokens(c: TokenConsumption) -> str:
    """The total always; the breakdown only when the vendor reported one, because a bucket printed
    as `0` where nobody counted reads as a measurement."""
    if c.input_tokens is None:
        return f"{c.consumed_tokens:,} tokens"
    return (
        f"{c.consumed_tokens:,} tokens "
        f"({c.input_tokens:,} in, {c.cache_read_tokens:,} cached, {c.output_tokens:,} out)"
    )
