"""The layer rules, enforced by walking the imports rather than trusting anyone to remember.

Three arrows, and each one is a design decision that would otherwise rot quietly:

1. `domain/` is pure — stdlib only. If a domain function needs a fact from the world, it takes it
   as an argument.
2. `domain/model/` may not import `domain/rules/`. The nouns do not know what the system decides
   about them; the verbs are free to depend on the nouns. This is the arrow that keeps `model/`
   inert and makes `rules/` the one place the design lives.
3. Nothing in `ralph/` imports from `tests/`. The fakes live under `tests/` precisely so that an
   adapter *cannot* reach for one.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import ralph
import ralph.domain

PACKAGE = Path(ralph.__file__).parent
DOMAIN = Path(ralph.domain.__file__).parent
DOMAIN_MODULES = sorted(DOMAIN.rglob("*.py"))
MODEL_MODULES = sorted((DOMAIN / "model").rglob("*.py"))
SHIPPED = sorted(PACKAGE.rglob("*.py"))

# Stdlib modules that nonetheless touch the world. Importing one in `domain/` is how purity rots.
BANNED = {"subprocess", "os", "io", "socket", "shutil", "asyncio", "pathlib", "tempfile"}


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
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ralph")
    }


def test_the_globs_found_something() -> None:
    """Guard against every test below passing vacuously."""
    assert len(DOMAIN_MODULES) > 5
    assert len(MODEL_MODULES) > 5
    assert len(SHIPPED) > 5


@pytest.mark.parametrize("module", DOMAIN_MODULES, ids=_rel)
def test_domain_imports_stdlib_only(module: Path) -> None:
    for root in _imported_roots(module):
        if root == "ralph":
            continue  # intra-domain; constrained further below
        assert root in sys.stdlib_module_names, f"{_rel(module)} imports third-party {root!r}"
        assert root not in BANNED, f"{_rel(module)} imports {root!r}, which touches the world"


@pytest.mark.parametrize("module", DOMAIN_MODULES, ids=_rel)
def test_domain_never_imports_outward(module: Path) -> None:
    """`domain/` may not import ports, adapters, or orchestration — only itself."""
    for imported in _ralph_imports(module):
        assert imported.startswith("ralph.domain"), (
            f"{_rel(module)} imports {imported!r} — that arrow points outward"
        )


@pytest.mark.parametrize("module", MODEL_MODULES, ids=_rel)
def test_the_nouns_do_not_know_about_the_verbs(module: Path) -> None:
    """`model/` may not import `rules/`.

    A value that knows how it will be classified has stopped being a value. Keeping this arrow
    one-way is what lets `rules/` be the single place the design lives — the taxonomy, the routing
    table, eligibility, the cycle cap — and keeps `model/` inert enough to be obviously correct.
    """
    for imported in _ralph_imports(module):
        assert not imported.startswith("ralph.domain.rules"), (
            f"{_rel(module)} imports {imported!r} — the nouns must not depend on the verbs"
        )


ORCHESTRATION = [PACKAGE / "mergequeue.py", PACKAGE / "scheduler.py", PACKAGE / "events.py"]


@pytest.mark.parametrize("module", ORCHESTRATION, ids=_rel)
def test_orchestration_never_names_a_concrete_adapter(module: Path) -> None:
    """`cli.py` is the composition root and the only module allowed to know that the Implementer
    is Codex, or that git is git. The moment the scheduler imports an adapter, the seam it was
    built around has stopped existing — and swapping Codex for Copilot becomes a code change in
    the scheduler.
    """
    for imported in _ralph_imports(module):
        assert not imported.startswith("ralph.adapters"), (
            f"{_rel(module)} imports {imported!r} — only cli.py may name an adapter"
        )


@pytest.mark.parametrize("module", SHIPPED, ids=_rel)
def test_the_shipped_package_never_imports_a_test(module: Path) -> None:
    """The fakes live under `tests/` so that this is enforceable at all. An adapter that reaches
    for a fake has stopped being an adapter."""
    for root in _imported_roots(module):
        assert root != "tests", f"{_rel(module)} imports from tests/ — that is not shippable"
