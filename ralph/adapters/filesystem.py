"""The issue tracker as it exists today: markdown files under `.scratch/<phase>/issues/`.

`LinearIssueStore` will implement the same Protocol and change nothing above it.

Every parse failure here is fatal and specific. A `Status:` line the harness does not recognise is
the single most dangerous thing this module could shrug at: a silent default would either run a
sub-issue the Planner never authorised, or skip one it did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ralph.domain import (
    Brief,
    Event,
    Findings,
    IssueGraph,
    SubIssue,
    SubIssueId,
    SubIssueState,
)

ISSUE_GLOB = "*.md"
_ID = re.compile(r"^(\d+)")
_STATUS = re.compile(r"^Status:\s*(\S+)\s*$", re.MULTILINE)
_HEADING = re.compile(r"^#\s+(.*)$", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$", re.MULTILINE)
_NUMBER = re.compile(r"\d+")
_FINDINGS = "## Findings"


class IssueParseError(ValueError):
    """The issue file does not say what the harness needs it to say. Always fatal, never guessed."""


@dataclass(frozen=True, slots=True)
class _ParsedIssue:
    id: SubIssueId
    title: str
    state: SubIssueState
    blocked_by: frozenset[SubIssueId]
    path: Path


def _section(body: str, heading: str) -> str:
    """The text under `heading`, up to the next `##`."""
    start = body.find(heading)
    if start == -1:
        return ""
    rest = body[start + len(heading) :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _parse_state(body: str, path: Path) -> SubIssueState:
    match = _STATUS.search(body)
    if match is None:
        raise IssueParseError(f"{path.name} has no `Status:` line")
    raw = match.group(1)
    try:
        return SubIssueState(raw)
    except ValueError:
        raise IssueParseError(
            f"{path.name} has `Status: {raw}`, which is not a sub-issue state. "
            f"The four are: {', '.join(s.value for s in SubIssueState)}. "
            "(`done` is the parent issue's state and is never written to a sub-issue.)"
        ) from None


def _parse_id(path: Path) -> SubIssueId:
    match = _ID.match(path.name)
    if match is None:
        raise IssueParseError(f"{path.name} does not begin with a numeric prefix")
    return SubIssueId(match.group(1))


def _parse_blockers(body: str, path: Path, siblings: dict[int, SubIssueId]) -> frozenset[SubIssueId]:
    """`#02`, `PDA #02`, and `02-remove-payload.md` all name sub-issue 02. `None` names nobody.

    Matched by *number*, not by string, so `#2` and `02` are the same sub-issue — which is what a
    human writing the file means, and the harness should not be the one to disagree.
    """
    section = _section(body, "## Blocked by")
    blockers: set[SubIssueId] = set()
    for bullet in _BULLET.findall(section):
        text = bullet.strip()
        if text.lower().startswith("none"):
            continue
        number = _NUMBER.search(text)
        if number is None:
            raise IssueParseError(f"{path.name}: blocker {text!r} names no sub-issue number")
        sibling = siblings.get(int(number.group()))
        if sibling is None:
            raise IssueParseError(
                f"{path.name}: blocked by {text!r}, but there is no sibling sub-issue "
                f"numbered {int(number.group())}"
            )
        blockers.add(sibling)
    return frozenset(blockers)


@dataclass(frozen=True, slots=True)
class FilesystemIssueStore:
    """`write_event` mirrors the state back into the `Status:` line. Best-effort by contract — the
    tracker is a convenience for humans, and the authoritative record is the `RunLog`."""

    issues_dir: Path

    def _files(self) -> list[Path]:
        files = sorted(self.issues_dir.glob(ISSUE_GLOB))
        if not files:
            raise IssueParseError(f"no {ISSUE_GLOB} sub-issues in {self.issues_dir}")
        return files

    def _parse_all(self) -> list[_ParsedIssue]:
        files = self._files()
        siblings = {int(_parse_id(p)): _parse_id(p) for p in files}

        parsed: list[_ParsedIssue] = []
        for path in files:
            body = path.read_text()
            heading = _HEADING.search(body)
            if heading is None:
                raise IssueParseError(f"{path.name} has no `# ` title")
            if "## Acceptance criteria" not in body:
                raise IssueParseError(
                    f"{path.name} has no `## Acceptance criteria` — an unattended agent has "
                    "nothing else to aim at"
                )
            parsed.append(
                _ParsedIssue(
                    id=_parse_id(path),
                    title=heading.group(1).strip(),
                    state=_parse_state(body, path),
                    blocked_by=_parse_blockers(body, path, siblings),
                    path=path,
                )
            )
        return parsed

    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]:
        parsed = self._parse_all()
        graph = IssueGraph(
            sub_issues={
                p.id: SubIssue(id=p.id, title=p.title, blocked_by=p.blocked_by) for p in parsed
            }
        )
        return graph, {p.id: p.state for p in parsed}

    def _path_of(self, id: SubIssueId) -> Path:
        for path in self._files():
            if _parse_id(path) == id:
                return path
        raise IssueParseError(f"no sub-issue {id!r} in {self.issues_dir}")

    def content(self, id: SubIssueId) -> tuple[Brief, Findings]:
        """The brief is the file. The findings are the one section the Editor owns."""
        body = self._path_of(id).read_text()
        return Brief(body=body), Findings(body=_section(body, _FINDINGS).strip())

    async def record_revision(self, id: SubIssueId, brief: Brief, findings: Findings) -> None:
        raise NotImplementedError("revisions land with the Editor loop, in sub-issue #07")

    async def write_event(self, e: Event) -> None:
        """Mirror a terminal state into the `Status:` line. Other events are the run log's job."""
        if e.kind != "terminal" or not isinstance(e.payload, SubIssueState):
            return
        path = self._path_of(e.sub_issue)
        body = path.read_text()
        path.write_text(_STATUS.sub(f"Status: {e.payload.value}", body, count=1))
