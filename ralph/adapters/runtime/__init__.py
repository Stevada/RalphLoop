"""The agent-agnostic session runtime: bound an actor session, run it over either transport, and
read it out per role.

`bounding.py` bounds a session on the wall clock; `session.py` and `turn_stream.py` are the two
transports (subprocess and SDK); `implementer.py` and `editor.py` are the two role cores that
interpret a run; `prompt.py` is what the actor is told. Nothing here names a concrete agent — the
vendor packages beside it (`claude/`, `codex/`, `copilot/`) supply that, building on this substrate.
"""
