"""Token consumption persisted per sub-issue session."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ralph.harness import Actor

CONSUMPTION_MARKER = "<!-- ralph:consumption -->"
_CONSUMPTION_LINE = re.compile(r"^\s*-\s+(implementer|editor):\s+(\d+)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SessionConsumption:
    actor: Actor
    consumed_tokens: int


def parse_consumption_records(body: str) -> tuple[SessionConsumption, ...]:
    return tuple(
        SessionConsumption(actor=Actor(actor), consumed_tokens=int(raw_tokens))
        for actor, raw_tokens in _CONSUMPTION_LINE.findall(body)
    )


def render_consumption_line(record: SessionConsumption) -> str:
    return f"- {record.actor.value}: {record.consumed_tokens}\n"
