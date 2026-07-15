"""The run log: append-only JSONL, one event per line.

Two kinds of thing and nothing else -- session states and Editor verdicts. **No token spend, no
diffstats, no failing-test output.** Those belong in the failure report, which has a different
reader: the run log answers *what happened, in what order*, and a reader who has to skim past a
40-line pytest dump to find the next event is not being told a story.

Append-only is not a performance choice. A run that rewrites an earlier line is a run that can
revise its own history, and the whole point of this file is that it cannot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ralph.domain import Actor, SubIssueId
from ralph.runlog.model import DETAILS_OF, Event, event_kind


class RunLogError(ValueError):
    """A line in the run log is not an event. Never skipped -- a log that quietly drops what it
    cannot parse is a log that lies by omission."""


@dataclass(frozen=True, slots=True)
class JsonlRunLog:
    path: Path

    async def write(self, e: Event) -> None:
        line = json.dumps(
            {
                "ts": e.ts.isoformat(),
                "sub_issue": str(e.sub_issue),
                "actor": e.actor.value,
                "kind": e.kind.value,
                "details": e.details.value,
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:  # "a", never "w". The file only ever grows.
            f.write(line + "\n")

    def events(self) -> tuple[Event, ...]:
        if not self.path.exists():
            return ()
        return tuple(
            self._parse(line, n)
            for n, line in enumerate(self.path.read_text().splitlines(), start=1)
            if line.strip()
        )

    def _parse(self, line: str, n: int) -> Event:
        try:
            raw = json.loads(line)
            kind = event_kind(raw["kind"])
            return Event(
                ts=datetime.fromisoformat(raw["ts"]),
                sub_issue=SubIssueId(raw["sub_issue"]),
                actor=Actor(raw["actor"]),
                kind=kind,
                details=DETAILS_OF[kind](raw["details"] if "details" in raw else raw["payload"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise RunLogError(f"{self.path}:{n} is not an event: {line!r}") from exc
