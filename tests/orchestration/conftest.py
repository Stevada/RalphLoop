from __future__ import annotations

import pytest

from ralph.ports import RepoCommands
from tests.fakes import FakeCommandSource

READY_COMMANDS = RepoCommands(test=("uv", "run", "pytest"), install=("uv", "sync"))


@pytest.fixture(autouse=True)
def command_source(monkeypatch: pytest.MonkeyPatch) -> FakeCommandSource:
    """Default command discovery for orchestration tests that are not about descriptor parsing."""
    source = FakeCommandSource(READY_COMMANDS)
    monkeypatch.setattr("ralph.cli.command_source_for", lambda: source)
    return source
