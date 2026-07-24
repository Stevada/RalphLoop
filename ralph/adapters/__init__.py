"""Adapters: the concrete things that satisfy the Protocols in `ralph.ports`.

The adapter-agnostic session runtime lives in `runtime/` — two transport spines (subprocess and SDK
turn-stream) under two role cores (Implementer and Editor), all bounded on the wall clock. Concrete
adapters build on it: Codex and Copilot each fill both roles over one SDK session; Claude Code fills
the Editor role. Each vendor adapter lives as a package beside `runtime/`.

Nothing here is imported by orchestration. `cli.py` is the only module that names one.
"""
