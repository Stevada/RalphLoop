# CLI Usage Telemetry

Reference for token-consumption telemetry exposed by each backend CLI. Consumption is recorded for
human visibility and cost accounting; it is not a session bound, and no harness decision gates on it.

## Codex

`codex exec --json` emits `turn.completed` on stdout at the end of the session. Ralph reads
`usage.total_tokens` from the last completed turn and stores it as `SessionTelemetry.consumed_tokens`.

## Copilot

The Copilot SDK emits usage as a structured event during the turn stream. Ralph records the
end-of-turn token total surfaced by `CopilotSdkSession`; no debug log is created or parsed for
consumption telemetry.
