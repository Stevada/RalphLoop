"""Which concrete adapter backs each role, and whether its runtime is on the machine.

This is the composition root proper: the only place that knows an Implementer named `codex` means
the Codex adapter. `options.py` resolves the name, this resolves the name into a thing.
"""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ralph.adapters.claude import claude_editor
from ralph.adapters.codex import codex_editor, codex_implementer, codex_integrator
from ralph.adapters.commands import DescriptorCommandSource
from ralph.adapters.copilot import (
    copilot_editor,
    copilot_implementer,
    copilot_integrator,
)
from ralph.cli.options import (
    CLAUDE,
    CODEX,
    COPILOT,
    EDITORS,
    IMPLEMENTERS,
    INTEGRATORS,
    NONE,
    RunOptions,
)
from ralph.ports import CommandSource, Editor, Implementer, Integrator

@dataclass(frozen=True, slots=True)
class ActorRuntime:
    """What a concrete adapter needs on the machine before any of its sessions can open."""

    kind: Literal["program", "module"]
    name: str
    """A CLI to find on PATH, or an SDK to import."""

    fix: str


ACTOR_RUNTIME: Mapping[str, ActorRuntime] = {
    CODEX: ActorRuntime("program", "codex", "install the Codex CLI and put `codex` on PATH"),
    CLAUDE: ActorRuntime("module", "claude_agent_sdk", "run `uv sync --extra editor`"),
    COPILOT: ActorRuntime("module", "copilot", "run `uv sync --extra copilot`"),
}
"""Here rather than in the adapters, because which actor is backed by which adapter is this module's
secret — and the pre-flight has to answer the question without constructing either one."""

class NoActor(RuntimeError):
    """The CLI named an Implementer or Editor the harness does not know. The harness will not
    invent one."""


def validate_actors(options: RunOptions) -> None:
    """The CLI must name actors this harness knows, without constructing their adapters."""
    if options.implementer not in IMPLEMENTERS:
        raise NoActor(
            f"implementer: {options.implementer!r} names no Implementer. "
            f"Known: {', '.join(IMPLEMENTERS)}."
        )
    if options.editor not in EDITORS:
        raise NoActor(
            f"editor: {options.editor!r} names no Editor. Known: {', '.join(EDITORS)}."
        )
    if options.integrator not in INTEGRATORS:
        raise NoActor(
            f"integrator: {options.integrator!r} names no Integrator. "
            f"Known: {', '.join(INTEGRATORS)}."
        )


def _installed(runtime: ActorRuntime) -> bool:
    if runtime.kind == "program":
        return shutil.which(runtime.name) is not None
    # `find_spec`, not an import: asking whether the SDK is *there* must not run a line of it.
    return importlib.util.find_spec(runtime.name) is not None


def actor_runtime_error(
    options: RunOptions,
    implementer: Implementer | None = None,
    editor: Editor | None = None,
    integrator: Integrator | None = None,
) -> str | None:
    """Every actor this run would *construct* whose runtime is missing, or `None` if there is none.

    Without this the failure surfaces at the first session that needs the runtime — which, for an
    Editor, is after a sub-issue has already failed and a wave of tokens has already been spent.

    A role handed a ready-made actor is skipped: nothing will be constructed for it, so what the
    options *name* for that role is not a fact about this run. A role switched off is skipped for
    the same reason — an absent Editor needs no SDK on the machine.
    """
    named_roles = [
        ("implementer", options.implementer, implementer),
        ("editor", options.editor, editor),
        ("integrator", options.integrator, integrator),
    ]
    missing = [
        f"{role} {named!r}: `{runtime.name}` is not installed — {runtime.fix}"
        for role, named, provided in named_roles
        if provided is None
        and named != NONE
        and not _installed(runtime := ACTOR_RUNTIME[named])
    ]
    return "; ".join(missing) if missing else None


def editor_of(options: RunOptions, suite: tuple[str, ...]) -> Editor | None:
    """Which model adjudicates a failed session, or `None` when the run has no Editor.

    The Editor and the Implementer should not be the same model on the same failure — an Editor
    adjudicating an impasse declared by *itself* is the least independent sensor the system could
    have. Nothing here enforces that; it is why two CLIs back each role.
    """
    named = options.editor
    if named == NONE:
        return None
    if named == CLAUDE:
        return claude_editor(suite=suite)
    if named == CODEX:
        return codex_editor(suite=suite)
    if named == COPILOT:
        return copilot_editor(suite=suite)
    validate_actors(options)
    raise AssertionError("validate_actors accepted an unknown Editor")


def integrator_of(options: RunOptions, git_metadata: Path) -> Integrator | None:
    """Which model reconciles a conflict, or `None` when the run has no Integrator. Its own flag
    because reconciling two correct trees is a different job from writing one, and worth being
    able to price differently."""
    named = options.integrator
    if named == NONE:
        return None
    if named == CODEX:
        return codex_integrator(git_metadata)
    if named == COPILOT:
        return copilot_integrator()
    validate_actors(options)
    raise AssertionError("validate_actors accepted an unknown Integrator")


def implementer_of(options: RunOptions, git_metadata: Path) -> Implementer:
    """Which model implements — the one decision only this module is allowed to make."""
    named = options.implementer
    if named == CODEX:
        return codex_implementer(git_metadata)
    if named == COPILOT:
        return copilot_implementer()
    validate_actors(options)
    raise AssertionError("validate_actors accepted an unknown Implementer")


def command_source_for() -> CommandSource:
    return DescriptorCommandSource()
