"""Adapters: the concrete things that satisfy the Protocols in `ralph.ports`.

Two transport spines — subprocess (`session.py`) and SDK turn-stream (`turn_stream.py`) — sit under
two role cores (`session.py` for the Implementer, `editor.py` for the Editor). A concrete agent maps
onto them: `codex.py` and `claude_editor.py` are single modules; Copilot, which fills both roles
over one SDK session, is the `copilot/` package.

Nothing here is imported by orchestration. `cli.py` is the only module that names one.
"""
