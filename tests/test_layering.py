"""The layer rules, enforced by walking the imports rather than trusting anyone to remember.

Three arrows, and each one is a design decision that would otherwise rot quietly:

1. `harness/` is pure — stdlib only. If a harness function needs a fact from the world, it takes it
   as an argument.
2. `harness/` may import the pure issue values, but not the issue store or filesystem adapter.
   Issue structure is an input to the harness's decisions; tracker I/O is not.
3. `harness/model/` may not import `harness/rules/`. The nouns do not know what the system decides
   about them; the verbs are free to depend on the nouns. This is the arrow that keeps `model/`
   inert and makes `rules/` the one place the design lives.
4. Nothing in `ralph/` imports from `tests/`. The fakes live under `tests/` precisely so that an
   adapter *cannot* reach for one.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import ralph
import ralph.harness
import ralph.notification

PACKAGE = Path(ralph.__file__).parent
HARNESS = Path(ralph.harness.__file__).parent
NOTIFICATION = Path(ralph.notification.__file__).parent
HARNESS_MODULES = sorted(HARNESS.rglob("*.py"))
NOTIFICATION_MODULES = sorted(NOTIFICATION.rglob("*.py"))
MODEL_MODULES = sorted((HARNESS / "model").rglob("*.py"))
SHIPPED = sorted(PACKAGE.rglob("*.py"))

# Stdlib modules that nonetheless touch the world. Importing one in `harness/` is how purity rots.
BANNED = {"subprocess", "os", "io", "socket", "shutil", "asyncio", "pathlib", "tempfile"}
ISSUE_VALUES = {"ralph.issues", "ralph.issues.content", "ralph.issues.graph", "ralph.issues.state"}
NOTIFICATION_INPUTS = ISSUE_VALUES | {
    "ralph.harness",
    "ralph.notification",
    "ralph.notification.assemble",
    "ralph.notification.model",
}


def _rel(p: Path) -> str:
    return str(p.relative_to(PACKAGE))


def _imported_roots(module: Path) -> set[str]:
    tree = ast.parse(module.read_text(), filename=str(module))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _ralph_imports(module: Path) -> set[str]:
    tree = ast.parse(module.read_text(), filename=str(module))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ralph")
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("ralph")
    )
    return imports


def test_the_globs_found_something() -> None:
    """Guard against every test below passing vacuously."""
    assert len(HARNESS_MODULES) > 5
    assert len(NOTIFICATION_MODULES) > 2
    assert len(MODEL_MODULES) > 5
    assert len(SHIPPED) > 5


@pytest.mark.parametrize("module", HARNESS_MODULES, ids=_rel)
def test_harness_imports_stdlib_only(module: Path) -> None:
    for root in _imported_roots(module):
        if root == "ralph":
            continue  # intra-harness; constrained further below
        assert root in sys.stdlib_module_names, f"{_rel(module)} imports third-party {root!r}"
        assert root not in BANNED, f"{_rel(module)} imports {root!r}, which touches the world"


@pytest.mark.parametrize("module", HARNESS_MODULES, ids=_rel)
def test_harness_never_imports_outward(module: Path) -> None:
    """`harness/` may not import ports, adapters, orchestration, or issue I/O."""
    for imported in _ralph_imports(module):
        assert imported.startswith("ralph.harness") or imported in ISSUE_VALUES, (
            f"{_rel(module)} imports {imported!r} — that arrow points outward"
        )


@pytest.mark.parametrize("module", NOTIFICATION_MODULES, ids=_rel)
def test_notification_is_pure(module: Path) -> None:
    """Notification assembly is a pure feature over harness failures and issue graph cost."""
    for root in _imported_roots(module):
        if root == "ralph":
            continue
        assert root in sys.stdlib_module_names, f"{_rel(module)} imports third-party {root!r}"
        assert root not in BANNED, f"{_rel(module)} imports {root!r}, which touches the world"


@pytest.mark.parametrize("module", NOTIFICATION_MODULES, ids=_rel)
def test_notification_only_depends_on_its_inputs(module: Path) -> None:
    """The notification feature may read the classification report and issue graph, not adapters."""
    for imported in _ralph_imports(module):
        assert imported in NOTIFICATION_INPUTS, (
            f"{_rel(module)} imports {imported!r} — notification should stay pure"
        )


@pytest.mark.parametrize("module", MODEL_MODULES, ids=_rel)
def test_the_nouns_do_not_know_about_the_verbs(module: Path) -> None:
    """`model/` may not import `rules/`.

    A value that knows how it will be classified has stopped being a value. Keeping this arrow
    one-way is what lets `rules/` be the single place the design lives — the taxonomy, the routing
    table, eligibility, the cycle cap — and keeps `model/` inert enough to be obviously correct.
    """
    for imported in _ralph_imports(module):
        assert not imported.startswith("ralph.harness.rules"), (
            f"{_rel(module)} imports {imported!r} — the nouns must not depend on the verbs"
        )


CLI = PACKAGE / "cli.py"
ISSUE_ADAPTERS = {"ralph.issues.filesystem", "ralph.issues.linear"}
ISSUE_ADAPTER_DIRS = {PACKAGE / "issues" / "filesystem", PACKAGE / "issues" / "linear"}
UPSTREAM = [
    m
    for m in SHIPPED
    if m != CLI and (PACKAGE / "adapters") not in m.parents and not ISSUE_ADAPTER_DIRS & set(m.parents)
]


@pytest.mark.parametrize("module", UPSTREAM, ids=_rel)
def test_only_the_composition_root_names_a_concrete_adapter(module: Path) -> None:
    """`cli.py` is the composition root and the only module allowed to know that the Implementer is
    Codex rather than Copilot, that the Editor is Claude Code rather than Copilot, or that git is
    git. The moment the scheduler imports an adapter, the seam it was built around has stopped
    existing — and swapping one model for another becomes a code change in the scheduler.

    Every module upstream of the adapters is walked, not a hand-kept list of three: the next
    orchestration module somebody adds is exactly the one a list would have failed to cover.
    """
    for imported in _ralph_imports(module):
        names_issue_adapter = any(
            imported == adapter or imported.startswith(f"{adapter}.") for adapter in ISSUE_ADAPTERS
        )
        assert not imported.startswith("ralph.adapters") and not names_issue_adapter, (
            f"{_rel(module)} imports {imported!r} — only cli.py may name an adapter"
        )


def test_the_composition_root_is_where_both_actors_are_chosen() -> None:
    """And the other half of the same rule: `cli.py` really does name them, so that the walk above
    is a statement about where the knowledge *lives* and not merely that nobody has it."""
    named = _ralph_imports(CLI)

    assert {"ralph.adapters.codex", "ralph.adapters.copilot"} <= named  # the Implementers
    assert {"ralph.adapters.claude_editor", "ralph.adapters.copilot"} <= named  # the Editors


def test_the_scheduler_asks_the_routing_table_rather_than_reimplementing_it() -> None:
    """The taxonomy is one table, in `rules/routing.py`, and the scheduler is a caller of it.

    The tempting alternative is an `if outcome is Outcome.CEILING_EXCEEDED or ...` in the pipeline,
    which works, passes, and quietly becomes a second routing table that drifts from the first. The
    failure mode is not a crash: it is the two disagreeing about `infra-failed` a year from now, and
    a session being handed to an Editor that can do nothing with it.
    """
    tree = ast.parse((PACKAGE / "scheduler.py").read_text())
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "route" in calls, "the scheduler decides where a session goes without asking `route`"


TESTS = Path(__file__).parent
ZERO_MOCKS = sorted(
    m for m in TESTS.glob("test_*.py") if "Zero mocks" in (ast.get_docstring(ast.parse(m.read_text())) or "")
)


def test_some_test_file_claims_zero_mocks() -> None:
    assert len(ZERO_MOCKS) >= 3


@pytest.mark.parametrize("module", ZERO_MOCKS, ids=lambda p: p.name)
def test_a_file_that_claims_zero_mocks_imports_no_fake(module: Path) -> None:
    """The end-to-end files say "Zero mocks" in their docstrings, and this is what makes that a
    claim rather than a boast: the sentence is read, and the imports are checked against it.

    Derived from the docstring rather than from a list kept here, so the next end-to-end file makes
    the promise and is held to it in the same breath — a list would have to be remembered, and the
    file that got left off it is exactly the one that would quietly start mocking git.
    """
    assert "tests.fakes" not in _imported_modules(module), (
        f"{module.name} says 'Zero mocks' in its docstring and then imports a fake"
    )


def _imported_modules(module: Path) -> set[str]:
    tree = ast.parse(module.read_text(), filename=str(module))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }


@pytest.mark.parametrize("module", SHIPPED, ids=_rel)
def test_the_shipped_package_never_imports_a_test(module: Path) -> None:
    """The fakes live under `tests/` so that this is enforceable at all. An adapter that reaches
    for a fake has stopped being an adapter."""
    for root in _imported_roots(module):
        assert root != "tests", f"{_rel(module)} imports from tests/ — that is not shippable"


@pytest.mark.parametrize("module", SHIPPED, ids=_rel)
def test_the_shipped_package_imports_harness_not_domain(module: Path) -> None:
    """The pure decision core is `ralph.harness`; `ralph.domain` is no longer a package."""
    for imported in _imported_modules(module):
        assert not (
            imported == "ralph.domain" or imported.startswith("ralph.domain.")
        ), f"{_rel(module)} imports {imported!r} — import from ralph.harness"
