# CLI Usage Telemetry

Reference for token-consumption telemetry exposed by each backend CLI. Consumption is recorded for
human visibility and cost accounting; it is not a session bound, and no harness decision gates on it.

## Codex

`codex exec --json` emits `turn.completed` on stdout at the end of the session. Ralph reads
`usage.total_tokens` from the last completed turn and stores it as `SessionTelemetry.consumed_tokens`.

## Copilot

Copilot writes usage blocks to its debug log when launched with `--log-dir` and `--log-level debug`.
Ralph reads the last completed `usage.total_tokens` from that log after the process exits.

The log is pretty-printed JSON embedded in timestamped text, so `json_blocks()` reconstructs parsed
objects before `usage_of()` reads the top-level `usage` key. That prevents unrelated JSON blocks,
such as model capability payloads, from being mistaken for session usage.

The log directory is fresh for every Copilot session and lives beside the worktree rather than
inside it, so a model told to commit its work cannot accidentally commit its own debug log.
