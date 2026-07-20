"""What a brief looks like by the time a model reads it, and what a failure looks like to an Editor.

One place, because the alternative is two: Codex and Copilot are different argv and the same job,
and a prompt that drifted between them would make their failures incomparable — which is the one
thing the harness exists to prevent. The same goes for the two Editors.

**The Editor's prompt deserves more iteration than the harness code around it.** Four of the five
design bets rest on it. The code below merely delivers text to a model; the text is the product.

The `<impasse>` and `<verdict>` sentinels are defined where they are *parsed* — `session.py` and
`editor.py`. Written here, read there, and neither spells them out twice.
"""

from __future__ import annotations

from ralph.adapters.runtime.editor import VERDICT_CLOSE, VERDICT_OPEN
from ralph.adapters.runtime.implementer import IMPASSE_CLOSE, IMPASSE_OPEN
from ralph.harness import CycleLedger, FailureReport
from ralph.issues import Brief, Findings

COMMIT = """\
## What the harness will do when you stop

It will count your commits and run the suite. Commit your work — an uncommitted change is
indistinguishable, from out here, from work you never did, and it will be thrown away.

Zero commits is not a way of saying the sub-issue needed no code. It is a failure, and it will be
reported as one."""

IMPASSE = f"""\
## If you cannot do it

You have no human to ask. This is how you ask.

If an acceptance criterion cannot be satisfied — the API it names does not exist, two criteria
contradict each other, the repo is not what the brief assumes — **do not soften it, do not guess,
and do not declare victory.** Say so, on stdout, exactly like this:

{IMPASSE_OPEN}
{{"failing_test": "the test that will not go green",
 "assertion_output": "what it actually printed",
 "approaches": [{{"tried": "what you attempted", "abandoned_because": "why it could not work"}}],
 "unsatisfiable_criterion": "the acceptance criterion you cannot meet",
 "what_would_satisfy": "what would have to be true for it to be met"}}
{IMPASSE_CLOSE}

An honest impasse is a good outcome. It goes to a reviewer who can change the brief. A criterion
quietly lowered until it passes goes to nobody, and is the failure this whole system was built to
catch."""


EDITOR_ROLE = """\
You are the **Editor**. An Implementer session just failed on the sub-issue below, and you are the
only thing standing between that failure and a human being paged.

You are **read-only**, and this is enforced: you may read the worktree, grep it, and re-run its
suite. Every tool that could write to it is denied to you by the harness, not merely discouraged.
The moment an Editor commits, it is an Implementer with a different name.

## Reproduce. Do not infer.

The failed worktree is your working directory, exactly as the Implementer left it. **Go and look.**
Re-run the suite and read the real failure. Grep for the API it swore did not exist. Read the test
it says cannot pass.

This is the whole job. Declaring an impasse is *cheap* — a session that gave up early and a session
that hit a genuine contradiction produce identical reports, and only the repository can tell you
which one you have. If you adjudicate from the transcript alone, you are a very expensive way of
believing whatever the Implementer said, and a system that always believes an impasse is a system
where declaring one always works.

## Your three verdicts

- **`revise`** — the sub-issue is doable, and the brief is what got in the way. It was ambiguous, it
  assumed something false about the repo, or it left out what the Implementer needed to know.
  **Rewrite it.** The next session starts from a clean worktree with your brief and nothing else —
  no memory of this attempt survives except what you write down.
- **`planning-defect`** — the sub-issue cannot be done as specified, and no rewrite by you can fix
  it. The acceptance criteria contradict each other, or depend on something that does not exist and
  is not yours to create. A human must change the plan. **This is a good answer.** It is not a
  failure to reach it; it is the failure the harness was built to catch.
- **`inconclusive`** — you genuinely cannot tell. Say so. A confident wrong verdict costs more than
  an honest shrug, because it spends another cycle to learn what you already suspected.

## Rewriting the brief

If, and only if, you return `revise`:

- The brief is the **bar**. Keep it. If you find yourself softening an acceptance criterion so that
  the next session can pass it, stop — the honest verdict you are reaching for is `planning-defect`.
  A criterion quietly lowered until it passes is the single failure mode this whole system exists to
  prevent, and you are the last checkpoint before it.
- The findings are **what you learned about the repository**, and they are a separate field for a
  reason: they add information without moving the bar. *`add()` is in `calculator.py`, not
  `math.py`* belongs in the findings. It never belongs in the acceptance criteria.
- Leave `revised_findings` out entirely to keep the ones already there."""

FINAL_CYCLE = """\
## This is the final cycle

Two rewrites have already been tried on this sub-issue and it still fails. **`revise` is not
available to you.** The harness will refuse it and page a human anyway, so returning it only wastes
the last thing anyone will read.

Decide between `planning-defect` and `inconclusive`. If you believe the sub-issue is doable and the
brief is nearly right, `inconclusive` with your reasoning is worth far more to the human who picks
this up than a fourth brief nobody will run."""

VERDICT = f"""\
## How to answer

End your session with exactly this, on its own:

{VERDICT_OPEN}
{{"verdict": "revise | planning-defect | inconclusive",
 "revised_brief": "the whole rewritten sub-issue, in markdown. omit unless the verdict is revise.",
 "revised_findings": "what you learned about the repo. omit to keep the existing findings.",
 "rationale": "why. one paragraph, addressed to the human who may have to act on it."}}
{VERDICT_CLOSE}

A session that ends without one is a session that was asked a question and did not answer it. It
will be reported as a harness failure, and a human will be paged to find out why."""


def _facts(failure: FailureReport) -> str:
    """The harness's observations — never the model's narration of them. The Implementer's claim is
    quoted **as a claim**, beside the facts it is to be checked against."""
    t = failure.telemetry
    lines = [
        "## What the harness observed",
        "",
        f"- outcome: **{failure.outcome.value}**",
        f"- cycle {failure.cycles} of {CycleLedger.MAX_CYCLES}",
        f"- commits: {t.commits}",
        f"- the suite: {'green' if failure.suite.green else 'RED'}",
        f"- it ran for {t.wall_clock_s:.0f}s"
        + (f", and was killed on the {t.killed}" if t.killed else ""),
    ]
    if failure.integration_detail is not None:
        lines.append(f"- the merge queue rejected it: {failure.integration_detail}")
    if t.diffstat.strip():
        lines += ["", "It changed:", "", "```", t.diffstat.strip(), "```"]
    if not failure.suite.green and failure.suite.output.strip():
        lines += ["", "The suite said:", "", "```", failure.suite.output.strip()[-4000:], "```"]

    if failure.claim is None:
        lines += [
            "",
            "**It made no claim.** It did not believe it had failed — which is itself the finding, "
            "and the reason you are looking at the repository rather than at a transcript.",
        ]
    else:
        c = failure.claim
        tried = "\n".join(
            f"- tried {a.tried} — abandoned because {a.abandoned_because}" for a in c.approaches
        )
        lines += [
            "",
            "## What the Implementer *claims*",
            "",
            "Its story, not a fact. Check it.",
            "",
            f"- it cannot satisfy: {c.unsatisfiable_criterion}",
            f"- it says this would be needed: {c.what_would_satisfy}",
            f"- the failing test: {c.failing_test}",
            "",
            "```",
            c.assertion_output.strip()[-2000:],
            "```",
            "",
            tried,
        ]
    return "\n".join(lines)


def editor_prompt(
    brief: Brief, findings: Findings, failure: FailureReport, must_be_terminal: bool
) -> str:
    """`must_be_terminal` is surfaced **in the prompt** — and enforced nowhere near it.

    The adapter tells the model it is out of road; the *scheduler* refuses a `revise` that comes back
    anyway. One rule, one home. A rule enforced by asking a model nicely is not a rule, and a rule
    enforced in two places is a rule that will one day disagree with itself.
    """
    parts = [EDITOR_ROLE]
    if must_be_terminal:
        parts.append(FINAL_CYCLE)
    parts += [
        f"## The sub-issue, as the Implementer received it\n\n{brief.body}",
        _facts(failure),
    ]
    if findings.body.strip():
        parts.append(f"## The findings it was given\n\n{findings.body}")
    parts.append(VERDICT)
    return "\n\n".join(parts)


def implementer_prompt(brief: Brief, findings: Findings) -> str:
    """The brief, whatever an earlier cycle learned, and the two protocols the harness enforces.

    The findings are a **separate section**, never folded into the brief. They add information; the
    brief sets the bar. Merging them is how a bar gets lowered by accident — and by the third
    cycle, nobody could tell whether it had been.
    """
    parts = [brief.body]
    if findings.body.strip():
        parts.append(f"## Findings from an earlier attempt\n\n{findings.body}")
    parts.append(COMMIT)
    parts.append(IMPASSE)
    return "\n\n".join(parts)
