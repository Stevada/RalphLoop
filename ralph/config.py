"""The run's configuration, resolved once into one value.

`ralph.yaml` at the target repo's root holds repo-local commands: the suite and the install command
that prepares it. The CLI supplies run arguments such as issue mode, actors, and protected branches.
`.env` beside it holds the one **secret** Ralph reads, `LINEAR_API_KEY`, plus any target-repo
variables the suite inherits.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

# The one secret a run reads from the environment.
LINEAR_API_KEY = "LINEAR_API_KEY"

# The config filenames that mean the target repo uses pre-commit.
PRE_COMMIT_CONFIGS = (".pre-commit-config.yaml", ".pre-commit-config.yml")

# The two files a run reads, at the target repo's root.
CONFIG_FILE = "ralph.yaml"
ENV_FILE = ".env"

# Where Ralph writes worktrees into the target repo: live runs, and preserved failures.
ACTIVE = Path(".worktrees") / "active"
QUARANTINE = Path(".worktrees") / "failed"


class ConfigError(ValueError):
    """`ralph.yaml` is missing, unparseable, or missing a required command."""


@dataclass(frozen=True, slots=True)
class Config:
    """A run's resolved configuration: CLI arguments, file commands, and the one secret."""

    issue_mode: str
    implementer: str
    editor: str
    protected: frozenset[str]
    test_cmd: tuple[str, ...]
    install_cmd: tuple[str, ...]
    linear_api_key: str | None

    @classmethod
    def resolve(
        cls,
        repo: Path,
        env: Mapping[str, str] = os.environ,
        *,
        issue_mode: str,
        implementer: str,
        editor: str,
        protected: frozenset[str],
    ) -> Config:
        """CLI arguments, `ralph.yaml`'s required commands, and the one secret from `env`."""
        path = repo / CONFIG_FILE
        raw = _load_mapping(path)
        return cls(
            issue_mode=issue_mode,
            implementer=implementer,
            editor=editor,
            protected=protected,
            test_cmd=_cmd(raw, "test_cmd", path),
            install_cmd=_cmd(raw, "install_cmd", path),
            linear_api_key=env.get(LINEAR_API_KEY),
        )

    def loggable(self) -> str:
        """A one-line summary safe to print. The API key is reported as set/unset, never echoed."""
        return (
            f"issue_mode={self.issue_mode} "
            f"implementer={self.implementer} "
            f"editor={self.editor} "
            f"protected={{{', '.join(sorted(self.protected))}}} "
            f"test_cmd={shlex.join(self.test_cmd)} "
            f"install_cmd={shlex.join(self.install_cmd)} "
            f"linear_api_key={'set' if self.linear_api_key else 'unset'}"
        )


def _load_mapping(path: Path) -> Mapping[str, object]:
    if not path.exists():
        raise ConfigError(f"{path}: no {CONFIG_FILE} at the repo root. Every run needs one.")
    loaded = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level, got {_kind(loaded)}")
    return loaded


def _cmd(raw: Mapping[str, object], key: str, path: Path) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        raise ConfigError(f"{path}: {key} is a required command.")
    if isinstance(value, str):
        return tuple(shlex.split(value))
    return tuple(_strlist(value, key, path))


def _strlist(value: object, key: str, path: Path) -> list[str]:
    if isinstance(value, list):
        items = [item for item in value if isinstance(item, str)]
        if len(items) == len(value):
            return items
    raise ConfigError(f"{path}: {key} must be a string or a list of strings")


def _kind(value: object) -> str:
    return type(value).__name__
