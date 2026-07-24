# CLI Usage Telemetry

Reference for token-consumption telemetry exposed by each backend CLI. Consumption is recorded for
human visibility and cost accounting; it is not a session bound, and no harness decision gates on it.

## Codex

`codex exec --json` emits JSONL events during the session. Ralph's Codex turn-stream wrapper reads
`usage.total_tokens` from completed-turn events and stores it as
`SessionTelemetry.consumed_tokens`. The same wrapper captures context-compaction events as
auto-compaction telemetry for the persistence path that consumes SDK session metadata.

## Copilot

The Copilot SDK emits usage as a structured event during the turn stream. Ralph records the
end-of-turn token total surfaced by `CopilotSdkSession`; no debug log is created or parsed for
consumption telemetry.
