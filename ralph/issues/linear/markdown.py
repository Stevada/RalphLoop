"""Ralph's markdown inside Linear issue descriptions and comments."""

from __future__ import annotations

import re

from ralph.harness import Actor, TokenConsumption
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
    r"(?:\s+tokens,\s+(\d+)\s+auto-compactions)?"
    r"(?:,\s+(\d+)\s+in,\s+(\d+)\s+cached,\s+(\d+)\s+out)?\s*$",
    re.MULTILINE,
)
"""The sub-issue is named by the comment's own issue, so it is absent from the line itself.

Both trailing groups are optional because this parses back what *earlier* runs wrote: a Linear issue
outlives the version of Ralph that commented on it, and a line missing the breakdown is a line from
before there was one — not a parse error.
"""


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
            consumption=_parsed_consumption(raw_tokens, raw_input, raw_cache_read, raw_output),
            auto_compactions=int(raw_auto_compactions or 0),
        )
        for actor, raw_tokens, raw_auto_compactions, raw_input, raw_cache_read, raw_output in (
            CONSUMPTION_LINE.findall(body)
        )
    )


def _parsed_consumption(
    raw_tokens: str, raw_input: str, raw_cache_read: str, raw_output: str
) -> TokenConsumption:
    """A line with no breakdown yields a total and no buckets — which is exactly what it says."""
    if not (raw_input and raw_cache_read and raw_output):
        return TokenConsumption.total_only(int(raw_tokens))
    return TokenConsumption.split(
        input=int(raw_input), cache_read=int(raw_cache_read), output=int(raw_output)
    )


def render_consumption_line(record: SessionConsumption) -> str:
    c = record.consumption
    breakdown = (
        ""
        if c.input_tokens is None or c.cache_read_tokens is None or c.output_tokens is None
        else f", {c.input_tokens} in, {c.cache_read_tokens} cached, {c.output_tokens} out"
    )
    return (
        f"- {record.actor.value}: {c.consumed_tokens} tokens, "
        f"{record.auto_compactions} auto-compactions{breakdown}\n"
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
