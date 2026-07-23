"""The filesystem issue tracker adapter: markdown files under `.scratch/<phase>/issues/`.

Every parse failure here is fatal and specific. A `Status:` line the harness does not recognise is
the single most dangerous thing this module could shrug at: a silent default would either run a
sub-issue the Planner never authorised, or skip one it did.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ralph.harness import Actor
from ralph.issues.content import Findings, Spec
from ralph.issues.consumption import SessionConsumption
from ralph.issues.graph import IssueGraph, SubIssue, SubIssueId
from ralph.issues.markdown import ACCEPTANCE_HEADING, FINDINGS_HEADING, section
from ralph.issues.state import SubIssueState
from ralph.runlog import Event, EventKind

ISSUE_GLOB = "*.md"
_ID = re.compile(r"^(\d+)")
_STATUS = re.compile(r"^Status:\s*(\S+)\s*$", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$", re.MULTILINE)
_NUMBER = re.compile(r"\d+")
_BLOCKED_BY_HEADING = "## Blocked by"  # filesystem-only: Linear has native blocking relations

REVISIONS = "revisions"
"""Where the Editor's rewrites live, beside the sub-issue files rather than on top of them.

A directory, not a suffix on the `.md`, so that `_files()`'s `*.md` glob — which is what defines
the graph — never sees a revision and mistakes it for a sub-issue.
"""

CONSUMPTION = "consumption.jsonl"
"""One append-only file for every sub-issue, outside the live spec so telemetry is durable but not
prompt material for the next Implementer session.

Not `.md`, and not negotiable: a markdown file here would be swept up by `_files()`'s glob and
parsed as a sub-issue. The sub-issue rides on each line, since the file no longer names one.
"""


class IssueParseError(ValueError):
    """The issue file does not say what the harness needs it to say. Always fatal, never guessed."""


@dataclass(frozen=True, slots=True)
class _ParsedIssue:
    id: SubIssueId
    state: SubIssueState
    blocked_by: frozenset[SubIssueId]
    path: Path


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
    blocked_by = section(body, _BLOCKED_BY_HEADING)
    blockers: set[SubIssueId] = set()
    for bullet in _BULLET.findall(blocked_by):
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
            if ACCEPTANCE_HEADING not in body:
                raise IssueParseError(
                    f"{path.name} has no `{ACCEPTANCE_HEADING}` — an unattended agent has "
                    "nothing else to aim at"
                )
            parsed.append(
                _ParsedIssue(
                    id=_parse_id(path),
                    state=_parse_state(body, path),
                    blocked_by=_parse_blockers(body, path, siblings),
                    path=path,
                )
            )
        return parsed

    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]:
        parsed = self._parse_all()
        graph = IssueGraph(
            sub_issues={p.id: SubIssue(id=p.id, blocked_by=p.blocked_by) for p in parsed}
        )
        return graph, {p.id: p.state for p in parsed}

    def _path_of(self, id: SubIssueId) -> Path:
        for path in self._files():
            if _parse_id(path) == id:
                return path
        raise IssueParseError(f"no sub-issue {id!r} in {self.issues_dir}")

    def _revisions_of(self, id: SubIssueId) -> Path:
        return self.issues_dir / REVISIONS / str(id)

    def _latest_revision(self, id: SubIssueId) -> int | None:
        """The highest revision on disk, or None when the Editor has never touched this sub-issue."""
        dir = self._revisions_of(id)
        if not dir.is_dir():
            return None
        numbers = [int(n.group()) for p in dir.glob("*-spec.md") if (n := _ID.match(p.name))]
        return max(numbers) if numbers else None

    def _planners_original(self, id: SubIssueId) -> tuple[Spec, Findings]:
        """Revision 0: the sub-issue file exactly as the Planner wrote it. The spec is the file;
        the findings are the one section the Editor owns."""
        body = self._path_of(id).read_text()
        return Spec(body=body), Findings(body=section(body, FINDINGS_HEADING).strip())

    def _consumption_path(self) -> Path:
        return self.issues_dir / CONSUMPTION

    def content(self, id: SubIssueId) -> tuple[Spec, Findings]:
        """What the next Implementer session works from: the newest revision, or the Planner's
        original when there is none.

        Spec and findings round-trip as **separate** fields, which is the point of storing them in
        separate files. A revision may change one and leave the other alone — the findings are a
        channel for adding information *without* lowering the bar, and conflating them with the
        spec is exactly how a bar gets lowered by accident.
        """
        latest = self._latest_revision(id)
        if latest is None:
            return self._planners_original(id)
        dir = self._revisions_of(id)
        return (
            Spec(body=(dir / f"{latest}-spec.md").read_text(), revision=latest),
            Findings(body=(dir / f"{latest}-findings.md").read_text()),
        )

    def consumption(self, id: SubIssueId) -> tuple[SessionConsumption, ...]:
        self._path_of(id)
        path = self._consumption_path()
        if not path.exists():
            return ()
        records = []
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            sub_issue, record = self._parse_consumption(line, n)
            if sub_issue == id:
                records.append(record)
        return tuple(records)

    def _parse_consumption(self, line: str, n: int) -> tuple[SubIssueId, SessionConsumption]:
        try:
            raw = json.loads(line)
            return SubIssueId(raw["sub_issue"]), SessionConsumption(
                actor=Actor(raw["actor"]),
                consumed_tokens=int(raw["consumed_tokens"]),
                auto_compactions=int(raw["auto_compactions"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise IssueParseError(
                f"{self._consumption_path()}:{n} is not a consumption record: {line!r}"
            ) from exc

    async def record_revision(self, id: SubIssueId, spec: Spec, findings: Findings) -> None:
        """Written **alongside** the Planner's original, never over it.

        Revision 0 is what a human diffs against to see whether the spec drifted — whether three
        rounds of Editor revision quietly softened "reject the request" into "log a warning". If the
        harness overwrote the spec in place, the evidence for the one bet most likely to fail
        (`docs/prd.md` §8: spec drift) would be destroyed by the very mechanism under suspicion.

        So revision 0 is snapshotted here, on the first revision, before anything is written. The
        live `.md` file keeps its own life — the `Status:` line is mirrored into it — and the
        snapshot is the frozen copy that stays diffable.

        The revision **number is the store's to assign**, not the Editor's. An Editor that could
        choose its own could overwrite an earlier one, and the store is the only thing that knows
        what is already on disk.
        """
        dir = self._revisions_of(id)
        dir.mkdir(parents=True, exist_ok=True)

        latest = self._latest_revision(id)
        if latest is None:
            original_spec, original_findings = self._planners_original(id)
            (dir / "0-spec.md").write_text(original_spec.body)
            (dir / "0-findings.md").write_text(original_findings.body)
            latest = 0

        next = latest + 1
        (dir / f"{next}-spec.md").write_text(spec.body)
        (dir / f"{next}-findings.md").write_text(findings.body)

    async def record_consumption(self, id: SubIssueId, record: SessionConsumption) -> None:
        self._path_of(id)
        line = json.dumps(
            {
                "sub_issue": str(id),
                "actor": record.actor.value,
                "consumed_tokens": record.consumed_tokens,
                "auto_compactions": record.auto_compactions,
            }
        )
        path = self._consumption_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:  # "a", never "w". The file only ever grows.
            handle.write(line + "\n")

    async def write_event(self, e: Event) -> None:
        """Mirror a terminal state into the `Status:` line. Other events are the run log's job."""
        if e.kind is not EventKind.SUB_ISSUE_CLOSED or not isinstance(e.details, SubIssueState):
            return
        path = self._path_of(e.sub_issue)
        body = path.read_text()
        path.write_text(_STATUS.sub(f"Status: {e.details.value}", body, count=1))

    async def publish_notification(self, body: str) -> None:
        """Filesystem runs already receive the notification through stdout."""
        return None
