"""CLI-owned run options."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ralph.cli import ENV_FILE, _load_env, _options_for, validate_agents


def test_cli_options_have_the_current_defaults() -> None:
    options = _options_for({})

    assert options.issue_mode == "filesystem"
    assert options.implementer == "codex"
    assert options.editor == "claude"
    assert options.protected == frozenset({"main", "master"})
    assert options.test_cmd == ("uv", "run", "pytest", "-q")
    assert options.install_cmd == ("uv", "sync")


def test_command_options_are_split_like_shell_words() -> None:
    options = _options_for({}, test_cmd="uv run pytest 'tests/a test.py'", install_cmd="uv sync")

    assert options.test_cmd == ("uv", "run", "pytest", "tests/a test.py")
    assert options.install_cmd == ("uv", "sync")


def test_codex_is_a_known_editor() -> None:
    validate_agents(_options_for({}, editor="codex"))


def test_a_dotenv_is_loaded_wholesale_without_policing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LINEAR_API_KYE", raising=False)
    (tmp_path / ENV_FILE).write_text("DATABASE_URL=postgres://x\nLINEAR_API_KYE=oops\n")
    try:
        _load_env(tmp_path)
        assert os.environ["DATABASE_URL"] == "postgres://x"
    finally:
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("LINEAR_API_KYE", None)
