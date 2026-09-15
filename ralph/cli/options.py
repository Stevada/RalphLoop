"""The run's arguments: the flags, the values they resolve to, and the `.env` beside them.

The actor names here are strings on purpose. Which adapter each one reaches for is the composition
root's business (`actors.py`); at this layer an actor is only a choice the user typed.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

CODEX = "codex"
CLAUDE = "claude"
COPILOT = "copilot"
NONE = "none"
"""The role is unfilled on this run. Only the two roles that answer a *failure* may be switched off
— an Implementer is the run, and a run without one has nothing to do."""

IMPLEMENTERS = (CODEX, COPILOT)
EDITORS = (CLAUDE, CODEX, COPILOT, NONE)
INTEGRATORS = (CODEX, COPILOT, NONE)


FILESYSTEM = "filesystem"
LINEAR = "linear"

DEFAULT_ISSUE_MODE = FILESYSTEM
DEFAULT_IMPLEMENTER = CODEX
DEFAULT_EDITOR = NONE
DEFAULT_INTEGRATOR = NONE
DEFAULT_PROTECTED = ("main", "master")

LINEAR_API_KEY = "LINEAR_API_KEY"
ENV_FILE = ".env"
# Operational verbosity is a command-line flag: the harness's own diagnostic
# log, separate from the run log. `WARNING` keeps a clean run quiet.
DEFAULT_LOG_LEVEL = "WARNING"


@dataclass(frozen=True, slots=True)
class RunOptions:
    """The run's resolved CLI options and one secret."""

    issue_mode: str = DEFAULT_ISSUE_MODE
    implementer: str = DEFAULT_IMPLEMENTER
    editor: str = DEFAULT_EDITOR
    integrator: str = DEFAULT_INTEGRATOR
    protected: frozenset[str] = frozenset(DEFAULT_PROTECTED)
    linear_api_key: str | None = None

    def loggable(self) -> str:
        return (
            f"issue_mode={self.issue_mode} "
            f"implementer={self.implementer} "
            f"editor={self.editor} "
            f"integrator={self.integrator} "
            f"protected={{{', '.join(sorted(self.protected))}}} "
            f"linear_api_key={'set' if self.linear_api_key else 'unset'}"
        )


def options_for(
    env: Mapping[str, str] = os.environ,
    *,
    issue_mode: str = DEFAULT_ISSUE_MODE,
    implementer: str = DEFAULT_IMPLEMENTER,
    editor: str = DEFAULT_EDITOR,
    integrator: str = DEFAULT_INTEGRATOR,
    protected: Sequence[str] = DEFAULT_PROTECTED,
) -> RunOptions:
    return RunOptions(
        issue_mode=issue_mode,
        implementer=implementer,
        editor=editor,
        integrator=integrator,
        protected=frozenset(protected),
        linear_api_key=env.get(LINEAR_API_KEY),
    )


def _add_option_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--issue-mode",
        choices=(FILESYSTEM, LINEAR),
        default=DEFAULT_ISSUE_MODE,
        help=f"how to interpret issue_source. Defaults to {DEFAULT_ISSUE_MODE}.",
    )
    parser.add_argument(
        "--implementer",
        choices=IMPLEMENTERS,
        default=DEFAULT_IMPLEMENTER,
        help=f"the CLI that writes code. Defaults to {DEFAULT_IMPLEMENTER}.",
    )
    parser.add_argument(
        "--editor",
        choices=EDITORS,
        default=DEFAULT_EDITOR,
        help=(
            f"the CLI that diagnoses failures. Defaults to {DEFAULT_EDITOR}. "
            f"{NONE} sends every failure it would have adjudicated to the human instead."
        ),
    )
    parser.add_argument(
        "--integrator",
        choices=INTEGRATORS,
        default=DEFAULT_INTEGRATOR,
        help=(
            f"the CLI that reconciles merge conflicts. Defaults to {DEFAULT_INTEGRATOR}. "
            f"{NONE} sends every merge conflict to the human instead."
        ),
    )
    parser.add_argument(
        "--protected",
        action="append",
        default=None,
        metavar="BRANCH",
        help="branch a run refuses to start from. Repeat for more. Defaults to main and master.",
    )


def load_env(repo: Path) -> None:
    """Load `<repo>/.env` into the environment — the one secret Ralph reads (`LINEAR_API_KEY`) and
    the target repo's own variables alike, for the subprocesses that inherit it. A real export still
    wins: the file is the default, the ambient environment the override. Nothing here is policed by
    name; the one secret is absent-checked where it is used, only on an `issue_mode: linear` run."""
    env_file = repo / ENV_FILE
    if env_file.exists():
        load_dotenv(env_file)
