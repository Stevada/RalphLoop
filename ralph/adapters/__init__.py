"""Adapters: the concrete things that satisfy the Protocols in `ralph.ports`.

Two transport spines — subprocess (`session.py`) and SDK turn-stream (`turn_stream.py`) — sit under
two role cores (`session.py` for the Implementer, `editor.py` for the Editor). Concrete agents map
onto them: Codex and Copilot each fill both roles over one SDK session and live as packages;
Claude Code, which only edits, remains `claude_editor.py`.

Nothing here is imported by orchestration. `cli.py` is the only module that names one.
"""
