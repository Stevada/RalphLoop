"""Consumption telemetry persisted per sub-issue session."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ralph.harness import Actor

CONSUMPTION_MARKER = "<!-- ralph:consumption -->"
_CONSUMPTION_LINE = re.compile(
    r"^\s*-\s+(implementer|editor):\s+(\d+)"
    r"(?:\s+tokens,\s+(\d+)\s+auto-compactions)?\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class SessionConsumption:
    actor: Actor
    consumed_tokens: int
    auto_compactions: int = 0


def parse_consumption_records(body: str) -> tuple[SessionConsumption, ...]:
    return tuple(
        SessionConsumption(
            actor=Actor(actor),
            consumed_tokens=int(raw_tokens),
            auto_compactions=int(raw_auto_compactions or 0),
        )
        for actor, raw_tokens, raw_auto_compactions in _CONSUMPTION_LINE.findall(body)
    )


def render_consumption_line(record: SessionConsumption) -> str:
    return (
        f"- {record.actor.value}: {record.consumed_tokens} tokens, "
        f"{record.auto_compactions} auto-compactions\n"
    )
