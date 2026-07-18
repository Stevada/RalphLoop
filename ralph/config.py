"""The run's configuration, resolved once into one value.

`ralph.yaml` at the target repo's root holds the **arguments** — how the harness behaves: the issue
mode, which actors, which branches, which suite. **Every argument is required**: a value the
harness would otherwise guess is a value the human must state, so there are no defaults to drift and
nothing to detect. `.env` beside it holds the one **secret** a run reads, `LINEAR_API_KEY`, and the
target repo's own variables, which the suite inherits. No setting lives in both file and env, so
each has exactly one name and there is nothing to merge. `Config.resolve` is the only reader of
either.

Two things are deliberately *not* arguments here: the issue source is a per-run command-line value,
and operational verbosity is a command-line flag. Anything a run does not vary — how `codex` or
`copilot` is driven, the four Linear state names — is not an argument at all but hardcoded in the
adapter that owns it.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

# The one secret a run reads from the environment. Everything that is not a secret lives in
# `ralph.yaml` (arguments) or on the command line (per-run values), never here.
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
    """`ralph.yaml` is missing, unparseable, or missing a required argument. Raised, never defaulted
    past: a run configured by a file nobody can parse is a run that lies."""


@dataclass(frozen=True, slots=True)
class Config:
    """A run's resolved configuration: every `ralph.yaml` argument, and the one secret.

    Every field but the secret is required — `resolve` raises rather than default any of them. The
    secret is `None` when `.env` set no `LINEAR_API_KEY`; that is checked where it is used, only on
    a run whose `issue_mode` is `linear`.
    """

    issue_mode: str
    implementer: str
    editor: str
    protected: frozenset[str]
    test_cmd: tuple[str, ...]
    install_cmd: tuple[str, ...]
    linear_api_key: str | None

    @classmethod
    def resolve(cls, repo: Path, env: Mapping[str, str] = os.environ) -> Config:
        """`ralph.yaml`'s required arguments under `repo`, and the one secret from `env`."""
        path = repo / CONFIG_FILE
        raw = _load_mapping(path)
        return cls(
            issue_mode=_str(raw, "issue_mode", path),
            implementer=_str(raw, "implementer", path),
            editor=_str(raw, "editor", path),
            protected=_strset(raw, "protected", path),
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


def _str(raw: Mapping[str, object], key: str, path: Path) -> str:
    value = raw.get(key)
    if value is None:
        raise ConfigError(f"{path}: {key} is a required argument.")
    if not isinstance(value, str):
        raise ConfigError(f"{path}: {key} must be a string, got {_kind(value)}")
    return value


def _strset(raw: Mapping[str, object], key: str, path: Path) -> frozenset[str]:
    value = raw.get(key)
    if value is None:
        raise ConfigError(f"{path}: {key} is a required argument.")
    return frozenset(_strlist(value, key, path))


def _cmd(raw: Mapping[str, object], key: str, path: Path) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        raise ConfigError(f"{path}: {key} is a required argument.")
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
