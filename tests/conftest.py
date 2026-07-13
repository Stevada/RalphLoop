"""The two fixtures every ticket below #03 is tested with."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.testbed import StandInAgent, TargetRepo, make_stand_in_agent, make_target_repo


@pytest.fixture
def repo(tmp_path: Path) -> TargetRepo:
    """A throwaway git repo, rebuilt per test. `tmp_path` tears it down; nothing leaks between
    tests, including the worktrees a test leaves behind."""
    return make_target_repo(tmp_path / "target")


@pytest.fixture
def agent(tmp_path: Path) -> StandInAgent:
    """Lives beside the repo, not inside it — an agent that committed itself would be a fine
    joke and a terrible fixture."""
    return make_stand_in_agent(tmp_path)
