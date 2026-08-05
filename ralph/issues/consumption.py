"""Consumption telemetry persisted per sub-issue session."""

from __future__ import annotations

from dataclasses import dataclass

from ralph.harness import Actor, TokenConsumption


@dataclass(frozen=True, slots=True)
class SessionConsumption:
    actor: Actor
    consumption: TokenConsumption
    auto_compactions: int = 0
