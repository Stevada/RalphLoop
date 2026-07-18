"""Running the target repo's suite, and installing its dependencies.

**The suite result, not the exit code, is the outcome.** This is the module that makes that true:
the harness runs the tests itself, in the worktree, and does not ask the model how it went.

`--test-cmd` and `--install-cmd` are CLI options. The harness detects nothing and guesses nothing
beyond those defaults.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ralph.harness import SuiteResult


class InstallFailed(RuntimeError):
    """Dependency installation failed. Fatal — and never `|| true`.

    This is the failure that, unclassified, produces the system's most expensive story: every test
    fails with `Cannot find module`, the Implementer honestly declares an impasse, and an Opus
    Editor is paid to diagnose `npm ci`.
    """


async def _run(cmd: Sequence[str], cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def install_once(repo: Path, cmd: tuple[str, ...]) -> None:
    """In the base checkout, once per run. Never per worktree — N worktrees would mean N installs
    of the same tree, and the first failure would be discovered N times."""
    code, output = await _run(cmd, repo)
    if code != 0:
        raise InstallFailed(f"`{' '.join(cmd)}` exited {code} in {repo}:\n{output}")


@dataclass(frozen=True, slots=True)
class SubprocessTestRunner:
    """The suite command, from CLI options; run many times, in worktrees."""

    cmd: tuple[str, ...]

    async def run(self, dir: Path) -> SuiteResult:
        started = time.monotonic()
        code, output = await _run(self.cmd, dir)
        return SuiteResult(
            green=code == 0, output=output, duration_s=time.monotonic() - started
        )
