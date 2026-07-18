"""`Config.resolve` is the one place a run reads its two files — `ralph.yaml` and `.env`.

The value of a single reader is that the rest of the harness never has to ask what a raw file key
or env string means: it means whatever the resolver parsed it into, once. So the parsing is what
these tests pin — that every argument is required (a missing one is a loud error, never a default),
that a command string becomes an argv, that the two files stay disjoint, and that the one secret is
never echoed by the summary meant for a log. A last test pins that `.env` is loaded wholesale: no
key is policed by name, so a target repo's own variables load beside Ralph's.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ralph.cli import _load_env
from ralph.config import CONFIG_FILE, ENV_FILE, Config, ConfigError

FULL = (
    "source: filesystem\n"
    "implementer: codex\n"
    "editor: claude\n"
    "protected: [main, master]\n"
    "test_cmd: uv run pytest -q\n"
    "install_cmd: [uv, sync]\n"
)


def _write(repo: Path, body: str) -> Path:
    (repo / CONFIG_FILE).write_text(body)
    return repo


def test_the_file_supplies_every_argument(tmp_path: Path) -> None:
    config = Config.resolve(_write(tmp_path, FULL), {})

    assert config.source == "filesystem"
    assert config.implementer == "codex"
    assert config.editor == "claude"
    assert config.protected == frozenset({"main", "master"})
    assert config.test_cmd == ("uv", "run", "pytest", "-q")  # a string is split into an argv
    assert config.install_cmd == ("uv", "sync")  # a list is taken verbatim


def test_a_missing_file_is_a_loud_error(tmp_path: Path) -> None:
    """No `ralph.yaml` is not "all defaults" — there are no defaults. Every run needs the file."""
    with pytest.raises(ConfigError, match=CONFIG_FILE):
        Config.resolve(tmp_path, {})


def test_a_missing_argument_is_a_loud_error(tmp_path: Path) -> None:
    """A configurable argument is required: dropping one fails the run rather than guessing a value.
    This is the old "no test suite detected" refusal, moved to where the argument is read."""
    without_suite = FULL.replace("test_cmd: uv run pytest -q\n", "")
    repo = _write(tmp_path, without_suite)

    with pytest.raises(ConfigError, match="test_cmd is a required argument"):
        Config.resolve(repo, {})


def test_the_environment_supplies_the_one_secret(tmp_path: Path) -> None:
    config = Config.resolve(_write(tmp_path, FULL), {"LINEAR_API_KEY": "lin_secret"})

    assert config.linear_api_key == "lin_secret"


def test_the_sources_are_disjoint_the_file_never_reads_an_env_argument(tmp_path: Path) -> None:
    """`implementer` is a `ralph.yaml` argument, full stop — there is no env var for it, so an
    ambient `RALPH_IMPLEMENTER` cannot reach into the run."""
    config = Config.resolve(_write(tmp_path, FULL), {"RALPH_IMPLEMENTER": "copilot"})

    assert config.implementer == "codex"


def test_a_malformed_argument_is_a_loud_error(tmp_path: Path) -> None:
    repo = _write(tmp_path, FULL.replace("protected: [main, master]\n", "protected: main\n"))

    with pytest.raises(ConfigError, match="protected"):
        Config.resolve(repo, {})


def test_the_loggable_summary_never_echoes_the_api_key(tmp_path: Path) -> None:
    """The summary is printed at run start; the API key is the one field that must not appear in a
    log, so it is reported only as set or unset."""
    summary = Config.resolve(_write(tmp_path, FULL), {"LINEAR_API_KEY": "lin_api_secret"}).loggable()

    assert "lin_api_secret" not in summary
    assert "linear_api_key=set" in summary


def test_a_dotenv_is_loaded_wholesale_without_policing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key is validated by name: a target repo's own variable loads for its suite, and even a
    Ralph-looking typo is accepted rather than rejected — the one secret is checked where it is
    used, only on a `source: linear` run, not by filtering the file."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LINEAR_API_KYE", raising=False)
    (tmp_path / ENV_FILE).write_text("DATABASE_URL=postgres://x\nLINEAR_API_KYE=oops\n")
    try:
        _load_env(tmp_path)  # does not raise on either key
        assert os.environ["DATABASE_URL"] == "postgres://x"
    finally:
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("LINEAR_API_KYE", None)
