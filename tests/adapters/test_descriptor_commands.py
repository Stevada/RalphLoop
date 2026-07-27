from __future__ import annotations

from pathlib import Path

import pytest

from ralph.adapters.commands import (
    DescriptorCommandSource,
    DescriptorCommandTypeError,
    DescriptorMissingError,
    DescriptorMissingTestError,
    DescriptorTomlError,
)
from ralph.ports import CommandSource


def write_descriptor(repo: Path, body: str) -> Path:
    descriptor = repo / ".ralph.toml"
    descriptor.write_text(body)
    return descriptor


def test_descriptor_commands_reads_test_and_install_commands(tmp_path: Path) -> None:
    write_descriptor(
        tmp_path,
        """
[commands]
test = "uv run pytest -k 'not slow'"
install = "uv sync --extra 'editor copilot'"
""".lstrip()
    )
    source: CommandSource = DescriptorCommandSource()

    commands = source.discover(tmp_path)

    assert commands.test == ("uv", "run", "pytest", "-k", "not slow")
    assert commands.install == ("uv", "sync", "--extra", "editor copilot")


def test_descriptor_commands_allows_no_install_command(tmp_path: Path) -> None:
    write_descriptor(
        tmp_path,
        """
[commands]
test = "uv run pytest"
""".lstrip()
    )

    commands = DescriptorCommandSource().discover(tmp_path)

    assert commands.test == ("uv", "run", "pytest")
    assert commands.install is None


def test_descriptor_commands_rejects_a_missing_descriptor(tmp_path: Path) -> None:
    descriptor = tmp_path / ".ralph.toml"

    with pytest.raises(DescriptorMissingError, match=rf"{descriptor}.*\.ralph\.toml"):
        DescriptorCommandSource().discover(tmp_path)


def test_descriptor_commands_rejects_malformed_toml(tmp_path: Path) -> None:
    descriptor = write_descriptor(tmp_path, "[commands\n")

    with pytest.raises(DescriptorTomlError, match=rf"{descriptor}.*TOML"):
        DescriptorCommandSource().discover(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "[tool.other]\nname = 'ralph'\n",
        "[commands]\n",
        "[commands]\ntest = ''\n",
        "[commands]\ntest = '   '\n",
    ],
)
def test_descriptor_commands_rejects_a_missing_or_empty_test(
    tmp_path: Path, body: str
) -> None:
    descriptor = write_descriptor(tmp_path, body)

    with pytest.raises(DescriptorMissingTestError, match=rf"{descriptor}.*commands\.test"):
        DescriptorCommandSource().discover(tmp_path)


@pytest.mark.parametrize(
    ("field", "body"),
    [
        ("commands.test", "[commands]\ntest = ['uv', 'run', 'pytest']\n"),
        ("commands.install", "[commands]\ntest = 'uv run pytest'\ninstall = 42\n"),
    ],
)
def test_descriptor_commands_rejects_non_string_command_values(
    tmp_path: Path, field: str, body: str
) -> None:
    descriptor = write_descriptor(tmp_path, body)

    with pytest.raises(DescriptorCommandTypeError, match=rf"{descriptor}.*{field}.*string"):
        DescriptorCommandSource().discover(tmp_path)
