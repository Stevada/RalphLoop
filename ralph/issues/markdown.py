"""The sub-issue markdown contract: the headings a Planner writes, and how a section is read.

Shared by both stores on purpose. The same `Findings` value round-trips through either, and the
same acceptance-criteria rule binds a Planner writing for either — so these cannot be allowed to
drift apart. Formats belonging to one medium (Linear's `## Spec` split of a single description
field, its comment markers) stay in the adapter that needs them.
"""

from __future__ import annotations

import re

FINDINGS_HEADING = "## Findings"
ACCEPTANCE_HEADING = "## Acceptance criteria"

_NEXT_HEADING = re.compile(r"\n##\s+")
"""`\\s+`, not a literal space: `##` followed by a tab is still a heading, and a section that
swallowed the next one would hand a session text the Planner filed under a different bar."""


def section(body: str, heading: str) -> str:
    """The text under `heading`, up to the next `##`. Empty when the heading is absent."""
    start = body.find(heading)
    if start == -1:
        return ""
    rest = body[start + len(heading) :]
    match = _NEXT_HEADING.search(rest)
    return rest if match is None else rest[: match.start()]
