"""Seeds a green suite from commit zero.

The merge queue refuses to land onto a red base, and `pytest` exits 5 — not 0 — when it
collects nothing at all. "No tests ran" is not green. Sub-issue 01 replaces this file with
the contract's real tests; until then it keeps the base honest.
"""


def test_the_suite_runs() -> None:
    assert True
