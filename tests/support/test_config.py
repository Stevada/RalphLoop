"""`Config.resolve` reads `ralph.yaml` commands and the one secret."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ralph.cli import _load_env
from ralph.config import CONFIG_FILE, ENV_FILE, Config, ConfigError

FULL = (
    "test_cmd: uv run pytest -q\n"
    "install_cmd: [uv, sync]\n"
)


def _write(repo: Path, body: str) -> Path:
    (repo / CONFIG_FILE).write_text(body)
    return repo


def _resolve(
    repo: Path,
    env: dict[str, str] | None = None,
    *,
    issue_mode: str = "filesystem",
    implementer: str = "codex",
    editor: str = "claude",
    protected: frozenset[str] = frozenset({"main", "master"}),
) -> Config:
    return Config.resolve(
        repo,
        env or {},
        issue_mode=issue_mode,
        implementer=implementer,
        editor=editor,
        protected=protected,
    )


def test_the_file_supplies_commands_and_the_cli_supplies_run_arguments(tmp_path: Path) -> None:
    config = _resolve(
        _write(tmp_path, FULL),
        issue_mode="linear",
        implementer="copilot",
        editor="copilot",
        protected=frozenset({"integration"}),
    )

    assert config.issue_mode == "linear"
    assert config.implementer == "copilot"
    assert config.editor == "copilot"
    assert config.protected == frozenset({"integration"})
    assert config.test_cmd == ("uv", "run", "pytest", "-q")  # a string is split into an argv
    assert config.install_cmd == ("uv", "sync")  # a list is taken verbatim


def test_a_missing_file_is_a_loud_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=CONFIG_FILE):
        _resolve(tmp_path)


@pytest.mark.parametrize(
    "line",
    [
        "test_cmd: uv run pytest -q\n",
        "install_cmd: [uv, sync]\n",
    ],
)
def test_a_missing_command_is_a_loud_error(tmp_path: Path, line: str) -> None:
    repo = _write(tmp_path, FULL.replace(line, ""))
    key = line.split(":", 1)[0]

    with pytest.raises(ConfigError, match=f"{key} is a required command"):
        _resolve(repo)


def test_the_environment_supplies_the_one_secret(tmp_path: Path) -> None:
    config = _resolve(_write(tmp_path, FULL), {"LINEAR_API_KEY": "lin_secret"})

    assert config.linear_api_key == "lin_secret"


def test_the_loggable_summary_never_echoes_the_api_key(tmp_path: Path) -> None:
    summary = _resolve(_write(tmp_path, FULL), {"LINEAR_API_KEY": "lin_api_secret"}).loggable()

    assert "lin_api_secret" not in summary
    assert "linear_api_key=set" in summary


def test_a_dotenv_is_loaded_wholesale_without_policing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LINEAR_API_KYE", raising=False)
    (tmp_path / ENV_FILE).write_text("DATABASE_URL=postgres://x\nLINEAR_API_KYE=oops\n")
    try:
        _load_env(tmp_path)  # does not raise on either key
        assert os.environ["DATABASE_URL"] == "postgres://x"
    finally:
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("LINEAR_API_KYE", None)
