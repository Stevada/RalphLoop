# CLI Usage Telemetry

Reference for token-consumption telemetry exposed by each backend CLI. Consumption is recorded for
human visibility and cost accounting; it is not a session bound, and no harness decision gates on it.

Every adapter reports the same three buckets, whatever its vendor calls them. Their meanings are
[`UBIQUITOUS_LANGUAGE.md`](../UBIQUITOUS_LANGUAGE.md)'s; what each vendor sends, and what it takes to
get there, is below. Two rules hold across all three:

- **Components before totals.** Where a vendor sends both, the components win — a total read in
  preference to a breakdown that was right there discards information for nothing.
- **A vendor that sends only a total leaves the buckets unknown**, never zero.

## Codex

`codex exec --json` emits JSONL events during the session, and the completed-turn event carries the
usage. Codex counts cached tokens **inside** `input_tokens`, so `input` is
`input_tokens - cached_input_tokens` and `cache_read` is `cached_input_tokens`. It publishes no
cache-write figure at all; those tokens are billed inside `input_tokens` and stay in `input`.
`reasoning_output_tokens` breaks down `output_tokens` rather than adding to it, so summing all four
fields double-counts. `usage.total_tokens` is the legacy path — releases since codex-cli 0.143.0
drop it and send components. The same wrapper captures context-compaction events as auto-compaction
telemetry.

## Claude

The Agent SDK bills **per message**, so the adapter accumulates: each turn it emits is the running
total, which is what the shared turn stream expects. `input_tokens` already excludes both cached
halves, so no subtraction is needed — but `cache_creation_input_tokens` is folded into `input`,
because a cache write costs close to fresh prompt while a cache read costs a fraction of it.

## Copilot

The SDK emits usage as a structured event during the turn stream; Ralph records the end-of-turn
total surfaced by `CopilotSdkSession`, and no debug log is created or parsed for consumption
telemetry. `cache_write_tokens` folds into `input` as it does for Claude, and `reasoning_tokens`
breaks down `output_tokens` rather than adding to it.
