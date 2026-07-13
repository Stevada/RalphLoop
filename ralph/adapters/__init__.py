"""Adapters: the concrete things that satisfy the Protocols in `ralph.ports`.

Flat on purpose, and **not** grouped by port: `copilot.py` is both an Implementer and an Editor,
so grouping by port would have to split it or duplicate it.

Nothing here is imported by orchestration. `cli.py` is the only module that names one.
"""
