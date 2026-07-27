"""Read the target repo's command descriptor."""

from __future__ import annotations

import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast

from ralph.ports import RepoCommands

DESCRIPTOR_NAME = ".ralph.toml"


class DescriptorCommandError(RuntimeError):
    """The target repo's command descriptor cannot be used."""


class DescriptorMissingError(DescriptorCommandError):
    """The target repo has no command descriptor."""


class DescriptorTomlError(DescriptorCommandError):
    """The command descriptor is not valid TOML."""


class DescriptorMissingTestError(DescriptorCommandError):
    """The descriptor does not declare a runnable test command."""


class DescriptorCommandTypeError(DescriptorCommandError):
    """A command field is present but is not a string."""


class DescriptorUntrackedError(DescriptorCommandError):
    """The command descriptor exists locally but is not tracked by git."""


@dataclass(frozen=True, slots=True)
class DescriptorCommandSource:
    def discover(self, repo: Path) -> RepoCommands:
        descriptor = repo / DESCRIPTOR_NAME
        if not descriptor.exists():
            raise DescriptorMissingError(
                f"{descriptor}: missing {DESCRIPTOR_NAME} command descriptor"
            )
        try:
            with descriptor.open("rb") as f:
                data: Mapping[str, object] = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise DescriptorTomlError(f"{descriptor}: malformed TOML: {e}") from e

        commands_value = data.get("commands")
        commands = (
            cast(Mapping[str, object], commands_value)
            if isinstance(commands_value, dict)
            else {}
        )
        return RepoCommands(
            test=_required_test(descriptor, commands),
            install=_optional_command(descriptor, commands, "install"),
        )


def _required_test(descriptor: Path, commands: Mapping[str, object]) -> tuple[str, ...]:
    value = commands.get("test")
    if value is None:
        raise DescriptorMissingTestError(f"{descriptor}: commands.test is required")
    if not isinstance(value, str):
        raise DescriptorCommandTypeError(f"{descriptor}: commands.test must be a string")
    if value.strip() == "":
        raise DescriptorMissingTestError(f"{descriptor}: commands.test must not be empty")
    return tuple(shlex.split(value))


def _optional_command(
    descriptor: Path, commands: Mapping[str, object], field: str
) -> tuple[str, ...] | None:
    value = commands.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DescriptorCommandTypeError(f"{descriptor}: commands.{field} must be a string")
    return tuple(shlex.split(value))
