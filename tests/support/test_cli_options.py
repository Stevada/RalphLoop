"""CLI-owned run options."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ralph.cli import ENV_FILE, main, _load_env, _options_for, validate_actors


def test_cli_options_have_the_current_defaults() -> None:
    options = _options_for({})

    assert options.issue_mode == "filesystem"
    assert options.implementer == "codex"
    assert options.editor == "claude"
    assert options.protected == frozenset({"main", "master"})
    assert options.linear_api_key is None


def test_retired_command_options_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["validate", str(tmp_path), "--test-cmd", "uv run pytest"])

    with pytest.raises(SystemExit):
        main(["validate", str(tmp_path), "--install-cmd", "uv sync"])


def test_codex_is_a_known_editor() -> None:
    validate_actors(_options_for({}, editor="codex"))


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
