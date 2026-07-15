"""Running the target repo's suite, and installing its dependencies.

**The suite result, not the exit code, is the outcome.** This is the module that makes that true:
the harness runs the tests itself, in the worktree, and does not ask the model how it went.

A repo with no detectable suite is a **loud, fatal error**. Returning a green `SuiteResult` for a
repo whose tests we could not find would make every classification downstream a lie, and it would
be the most expensive lie in the system: an undeclared impasse would become unreachable, every red
suite waved through as success.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ralph.harness import SuiteResult

TEST_CMD_ENV = "RALPH_TEST_CMD"
INSTALL_CMD_ENV = "RALPH_INSTALL_CMD"


class NoSuiteFound(RuntimeError):
    """No test command could be detected and none was given. Fatal, before any session starts."""


class InstallFailed(RuntimeError):
    """Dependency installation failed. Fatal — and never `|| true`.

    This is the failure that, unclassified, produces the system's most expensive story: every test
    fails with `Cannot find module`, the Implementer honestly declares an impasse, and an Opus
    Editor is paid to diagnose `npm ci`.
    """


def detect_test_cmd(repo: Path) -> tuple[str, ...]:
    """`RALPH_TEST_CMD` wins; then npm, pytest, make. No detection means no run."""
    override = os.environ.get(TEST_CMD_ENV)
    if override:
        return tuple(shlex.split(override))
    if (repo / "package.json").exists():
        return ("npm", "test")
    if (
        (repo / "pyproject.toml").exists()
        or any(repo.glob("test_*.py"))
        or (repo / "tests").is_dir()
    ):
        # `sys.executable`, not `python`: the interpreter running the harness is the one we know
        # exists. A bare `python` is a coin flip on which environment answers.
        return (sys.executable, "-m", "pytest", "-q")
    if (repo / "Makefile").exists() and "test:" in (repo / "Makefile").read_text():
        return ("make", "test")
    raise NoSuiteFound(
        f"no test suite detected in {repo} and no {TEST_CMD_ENV} set. "
        "The harness will not run a repo whose tests it cannot run."
    )


def detect_install_cmd(repo: Path) -> tuple[str, ...] | None:
    """`None` means nothing to install — which is a fact, not a failure."""
    override = os.environ.get(INSTALL_CMD_ENV)
    if override:
        return tuple(shlex.split(override))
    if (repo / "package.json").exists():
        return ("npm", "ci")
    return None


async def _run(cmd: Sequence[str], cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def install_once(repo: Path) -> None:
    """In the base checkout, once per run. Never per worktree — N worktrees would mean N installs
    of the same tree, and the first failure would be discovered N times."""
    cmd = detect_install_cmd(repo)
    if cmd is None:
        return
    code, output = await _run(cmd, repo)
    if code != 0:
        raise InstallFailed(f"`{' '.join(cmd)}` exited {code} in {repo}:\n{output}")


@dataclass(frozen=True, slots=True)
class SubprocessTestRunner:
    """Detected once, in the base checkout; run many times, in worktrees."""

    cmd: tuple[str, ...]

    async def run(self, dir: Path) -> SuiteResult:
        started = time.monotonic()
        code, output = await _run(self.cmd, dir)
        return SuiteResult(
            green=code == 0, output=output, duration_s=time.monotonic() - started
        )
