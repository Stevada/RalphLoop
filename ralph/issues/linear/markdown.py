"""Ralph's markdown inside Linear issue descriptions and comments."""

from __future__ import annotations

import re

from ralph.issues.content import Brief, Findings
from ralph.issues.linear.model import LinearComment, LinearIssue, LinearIssueStoreError

BRIEF_HEADING = "## Brief"
FINDINGS_HEADING = "## Findings"
ACCEPTANCE_HEADING = "## Acceptance criteria"
REVISION = re.compile(r"<!--\s*ralph:revision:(\d+)\s*-->")


def parse_content(issue: LinearIssue) -> tuple[str, str]:
    body = issue.description
    brief = _brief_section(body)
    findings = _section(body, FINDINGS_HEADING)
    if not brief:
        brief = _without_findings(body).strip()
    if ACCEPTANCE_HEADING not in brief:
        raise LinearIssueStoreError(
            f"{issue.identifier} has no `{ACCEPTANCE_HEADING}` in its Linear description"
        )
    return brief.strip(), findings.strip()


def render_description(brief: Brief, findings: Findings) -> str:
    return f"{BRIEF_HEADING}\n\n{brief.body.strip()}\n\n{FINDINGS_HEADING}\n\n{findings.body.strip()}\n"


def render_revision(revision: int, brief: Brief, findings: Findings) -> str:
    return (
        f"<!-- ralph:revision:{revision} -->\n"
        f"## Ralph revision {revision}\n\n"
        "### Brief\n\n"
        f"{brief.body.strip()}\n\n"
        "### Findings\n\n"
        f"{findings.body.strip()}\n"
    )


def revision_comments(comments: tuple[LinearComment, ...]) -> tuple[LinearComment, ...]:
    return tuple(comment for comment in comments if REVISION.search(comment.body))


def latest_revision(comments: tuple[LinearComment, ...]) -> int:
    versions = [
        int(match.group(1)) for comment in comments if (match := REVISION.search(comment.body))
    ]
    return max(versions) if versions else 0


def _brief_section(body: str) -> str:
    start = body.find(BRIEF_HEADING)
    if start == -1:
        return ""
    rest = body[start + len(BRIEF_HEADING) :]
    end = rest.find(FINDINGS_HEADING)
    return rest if end == -1 else rest[:end]


def _section(body: str, heading: str) -> str:
    start = body.find(heading)
    if start == -1:
        return ""
    rest = body[start + len(heading) :]
    match = re.search(r"\n##\s+", rest)
    return rest if match is None else rest[: match.start()]


def _without_findings(body: str) -> str:
    start = body.find(FINDINGS_HEADING)
    return body if start == -1 else body[:start]
