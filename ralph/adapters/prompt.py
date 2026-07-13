"""What a brief looks like by the time a model reads it.

One place, because the alternative is two: Codex and Copilot are different argv and the same job,
and a prompt that drifted between them would make their failures incomparable — which is the one
thing the harness exists to prevent.

The `<impasse>` sentinel is defined in `session.py`, where it is *parsed*. Written here, read
there, and neither spells it out twice.
"""

from __future__ import annotations

from ralph.adapters.session import IMPASSE_CLOSE, IMPASSE_OPEN
from ralph.domain import Brief, Findings

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
