"""Ralph's markdown inside Linear issue descriptions and comments."""

from __future__ import annotations

import re

from ralph.harness import Actor
from ralph.issues.consumption import SessionConsumption
from ralph.issues.content import Findings, Spec
from ralph.issues.linear.model import LinearComment, LinearIssue, LinearIssueStoreError
from ralph.issues.markdown import ACCEPTANCE_HEADING, FINDINGS_HEADING, section

SPEC_HEADING = "## Spec"
"""Linear-only: one description field holds both halves, so the spec needs a heading to end at."""

REVISION = re.compile(r"<!--\s*ralph:revision:(\d+)\s*-->")

CONSUMPTION_MARKER = "<!-- ralph:consumption -->"
CONSUMPTION_LINE = re.compile(
    r"^\s*-\s+(implementer|editor):\s+(\d+)"
    r"(?:\s+tokens,\s+(\d+)\s+auto-compactions)?\s*$",
    re.MULTILINE,
)
"""The sub-issue is named by the comment's own issue, so it is absent from the line itself."""


def parse_content(issue: LinearIssue) -> tuple[str, str]:
    body = issue.description
    spec = _spec_section(body)
    findings = section(body, FINDINGS_HEADING)
    if not spec:
        spec = _without_findings(body).strip()
    if ACCEPTANCE_HEADING not in spec:
        raise LinearIssueStoreError(
            f"{issue.identifier} has no `{ACCEPTANCE_HEADING}` in its Linear description"
        )
    return spec.strip(), findings.strip()


def render_description(spec: Spec, findings: Findings) -> str:
    return f"{SPEC_HEADING}\n\n{spec.body.strip()}\n\n{FINDINGS_HEADING}\n\n{findings.body.strip()}\n"


def render_revision(revision: int, spec: Spec, findings: Findings) -> str:
    return (
        f"<!-- ralph:revision:{revision} -->\n"
        f"## Ralph revision {revision}\n\n"
        "### Spec\n\n"
        f"{spec.body.strip()}\n\n"
        "### Findings\n\n"
        f"{findings.body.strip()}\n"
    )


def parse_consumption_records(body: str) -> tuple[SessionConsumption, ...]:
    return tuple(
        SessionConsumption(
            actor=Actor(actor),
            consumed_tokens=int(raw_tokens),
            auto_compactions=int(raw_auto_compactions or 0),
        )
        for actor, raw_tokens, raw_auto_compactions in CONSUMPTION_LINE.findall(body)
    )


def render_consumption_line(record: SessionConsumption) -> str:
    return (
        f"- {record.actor.value}: {record.consumed_tokens} tokens, "
        f"{record.auto_compactions} auto-compactions\n"
    )


def revision_comments(comments: tuple[LinearComment, ...]) -> tuple[LinearComment, ...]:
    return tuple(comment for comment in comments if REVISION.search(comment.body))


def latest_revision(comments: tuple[LinearComment, ...]) -> int:
    versions = [
        int(match.group(1)) for comment in comments if (match := REVISION.search(comment.body))
    ]
    return max(versions) if versions else 0


def _spec_section(body: str) -> str:
    start = body.find(SPEC_HEADING)
    if start == -1:
        return ""
    rest = body[start + len(SPEC_HEADING) :]
    end = rest.find(FINDINGS_HEADING)
    return rest if end == -1 else rest[:end]


def _without_findings(body: str) -> str:
    start = body.find(FINDINGS_HEADING)
    return body if start == -1 else body[:start]
