"""The run's configuration — every environment variable it reads, resolved once into one value.

`Config.from_env` is the **only** place `os.environ` is read: the composition root builds a
`Config` at the start of a run, and everything downstream takes the resolved value rather than
reaching for the environment itself. That is what makes "what is this run configured with" a
question with a single answer — a frozen record it can also print — instead of nine lazy
`os.environ.get` calls scattered across the factories.

The `RALPH_*` names and their defaults live here too, so the README env-var table and
`UBIQUITOUS_LANGUAGE.md` have a single code-side counterpart to point at.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Which actors run, and how they are invoked.
AGENT_CMD_ENV = "RALPH_AGENT_CMD"
IMPLEMENTER_ENV = "RALPH_IMPLEMENTER"
EDITOR_ENV = "RALPH_EDITOR"

# The target repo's suite and dependency install, when detection is not enough.
TEST_CMD_ENV = "RALPH_TEST_CMD"
INSTALL_CMD_ENV = "RALPH_INSTALL_CMD"

# The branches a run refuses to start from, and the override that names its own set.
PROTECTED_ENV = "RALPH_PROTECTED_BRANCHES"
DEFAULT_PROTECTED = ("main", "master")

# The config filenames that mean the target repo uses pre-commit.
PRE_COMMIT_CONFIGS = (".pre-commit-config.yaml", ".pre-commit-config.yml")

# Reading the issue graph from Linear instead of the filesystem.
LINEAR_API_KEY_ENV = "LINEAR_API_KEY"
LINEAR_PARENT_ENV = "RALPH_LINEAR_PARENT"
LINEAR_READY_ENV = "RALPH_LINEAR_STATE_READY"
LINEAR_IN_PROGRESS_ENV = "RALPH_LINEAR_STATE_IN_PROGRESS"
LINEAR_LANDED_ENV = "RALPH_LINEAR_STATE_LANDED"
LINEAR_NEEDS_HUMAN_ENV = "RALPH_LINEAR_STATE_NEEDS_HUMAN"

# Where Ralph writes worktrees into the target repo: live runs, and preserved failures.
ACTIVE = Path(".worktrees") / "active"
QUARANTINE = Path(".worktrees") / "failed"


@dataclass(frozen=True, slots=True)
class LinearStateOverrides:
    """The Linear workflow-state names a run overrides. `None` keeps the adapter's own default.

    Held as raw names, not as a `LinearStateMap`: this module is upstream of the adapters and may
    not name one. `cli.py` assembles the map — the defaults belong to the adapter that owns them.
    """

    ready: str | None
    in_progress: str | None
    landed: str | None
    needs_human: str | None


@dataclass(frozen=True, slots=True)
class Config:
    """A run's resolved configuration: the environment read once, defaults already applied.

    `test_cmd`/`install_cmd` are `None` when nothing was set — the detector then discovers them
    from the repo, because an override is the only part of suite selection that is configuration.
    """

    agent_cmd: str | None
    implementer: str | None
    editor: str | None
    protected: frozenset[str]
    test_cmd: tuple[str, ...] | None
    install_cmd: tuple[str, ...] | None
    linear_api_key: str | None
    linear_parent: str | None
    linear_states: LinearStateOverrides

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        return cls(
            agent_cmd=env.get(AGENT_CMD_ENV),
            implementer=env.get(IMPLEMENTER_ENV),
            editor=env.get(EDITOR_ENV),
            protected=_words(env.get(PROTECTED_ENV)) or frozenset(DEFAULT_PROTECTED),
            test_cmd=_argv(env.get(TEST_CMD_ENV)),
            install_cmd=_argv(env.get(INSTALL_CMD_ENV)),
            linear_api_key=env.get(LINEAR_API_KEY_ENV),
            linear_parent=env.get(LINEAR_PARENT_ENV),
            linear_states=LinearStateOverrides(
                ready=env.get(LINEAR_READY_ENV),
                in_progress=env.get(LINEAR_IN_PROGRESS_ENV),
                landed=env.get(LINEAR_LANDED_ENV),
                needs_human=env.get(LINEAR_NEEDS_HUMAN_ENV),
            ),
        )

    def loggable(self) -> str:
        """A one-line summary safe to print. The API key is reported as set/unset, never echoed."""
        return (
            f"implementer={self.implementer or 'stand-in (RALPH_AGENT_CMD)'} "
            f"editor={self.editor or 'none'} "
            f"protected={{{', '.join(sorted(self.protected))}}} "
            f"test_cmd={shlex.join(self.test_cmd) if self.test_cmd else 'detect'} "
            f"install_cmd={shlex.join(self.install_cmd) if self.install_cmd else 'detect'} "
            f"linear_parent={self.linear_parent or 'none'} "
            f"linear_api_key={'set' if self.linear_api_key else 'unset'}"
        )


def _argv(value: str | None) -> tuple[str, ...] | None:
    return tuple(shlex.split(value)) if value else None


def _words(value: str | None) -> frozenset[str]:
    return frozenset(shlex.split(value)) if value else frozenset()
