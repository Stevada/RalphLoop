"""`Config.from_env` is the one place a run reads its environment.

The value of a single reader is that the rest of the harness never has to ask what a raw env string
means: it means whatever `from_env` parsed it into, once. So the parsing is what these tests pin —
that a command string becomes an argv, that an unset knob falls back rather than fails, and that the
one secret in the record is never echoed by the summary meant for a log.
"""

from __future__ import annotations

from ralph.config import DEFAULT_PROTECTED, Config


def test_an_empty_environment_is_all_defaults() -> None:
    config = Config.from_env({})

    assert config.protected == frozenset(DEFAULT_PROTECTED)
    assert config.test_cmd is None  # nothing set: the detector discovers it from the repo
    assert config.install_cmd is None
    assert config.implementer is None and config.editor is None and config.agent_cmd is None
    assert config.linear_api_key is None and config.linear_parent is None
    assert config.linear_states.ready is None  # unset: the adapter's own default stands


def test_a_command_override_is_split_into_an_argv() -> None:
    config = Config.from_env({"RALPH_TEST_CMD": "cargo test --all"})

    assert config.test_cmd == ("cargo", "test", "--all")


def test_the_protected_set_is_the_words_of_the_override() -> None:
    config = Config.from_env({"RALPH_PROTECTED_BRANCHES": "integration trunk"})

    assert config.protected == frozenset({"integration", "trunk"})


def test_linear_state_names_are_overridden_only_where_given() -> None:
    config = Config.from_env({"RALPH_LINEAR_STATE_READY": "Todo"})

    assert config.linear_states.ready == "Todo"
    assert config.linear_states.landed is None  # untouched, so the default will stand


def test_the_loggable_summary_never_echoes_the_api_key() -> None:
    """The summary is printed at run start; the API key is the one field that must not appear in a
    log, so it is reported only as set or unset."""
    summary = Config.from_env({"LINEAR_API_KEY": "lin_api_secret"}).loggable()

    assert "lin_api_secret" not in summary
    assert "linear_api_key=set" in summary
