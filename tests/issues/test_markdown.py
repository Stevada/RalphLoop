"""The sub-issue markdown contract both stores parse against.

`section` had two implementations — one per store — and they had already drifted on where a section
ends. These pin the surviving behaviour, so the next divergence is a failing test rather than two
modes quietly disagreeing about what a Planner wrote.
"""

from __future__ import annotations

import pytest

from ralph.issues.markdown import ACCEPTANCE_HEADING, FINDINGS_HEADING, section


def test_a_section_runs_to_the_next_heading() -> None:
    body = f"{FINDINGS_HEADING}\n\nthe retry swallows it\n\n## Notes\n\nignore me\n"

    assert section(body, FINDINGS_HEADING).strip() == "the retry swallows it"


def test_a_section_runs_to_the_end_when_nothing_follows() -> None:
    body = f"{FINDINGS_HEADING}\n\nthe retry swallows it\n"

    assert section(body, FINDINGS_HEADING).strip() == "the retry swallows it"


def test_an_absent_heading_is_an_empty_section_not_the_whole_body() -> None:
    assert section(f"{ACCEPTANCE_HEADING}\n\n- [ ] It works.\n", FINDINGS_HEADING) == ""


@pytest.mark.parametrize("whitespace", [" ", "\t", "  ", "\n"])
def test_any_whitespace_after_the_hashes_still_ends_the_section(whitespace: str) -> None:
    """The drift the two implementations had: one ended a section only on `##` plus a single
    space, so a tab-indented heading was swallowed into the section above it — and a session would
    have been handed text the Planner filed under a different bar."""
    body = f"{FINDINGS_HEADING}\n\nthe retry swallows it\n\n##{whitespace}Notes\n\nignore me\n"

    assert "ignore me" not in section(body, FINDINGS_HEADING)
