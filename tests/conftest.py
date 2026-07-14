"""The two fixtures every ticket below #03 is tested with."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.testbed import StandInAgent, TargetRepo, make_stand_in_agent, make_target_repo


@pytest.fixture(autouse=True)
def no_model_in_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """**No test in this suite calls a model.**

    `RALPH_IMPLEMENTER` and `RALPH_EDITOR` are read from the environment by `cli.py`, which means a
    developer who has `RALPH_EDITOR=claude` exported for their own runs would silently have the whole
    end-to-end suite billing Opus. Cleared here for every test, once, rather than remembered in each
    one — a safety property that depends on being remembered is not one.
    """
    monkeypatch.delenv("RALPH_IMPLEMENTER", raising=False)
    monkeypatch.delenv("RALPH_EDITOR", raising=False)


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
