# 05 — Auto-compaction telemetry

Status: landed

## What to build

Land the field Phase A deferred: **auto-compaction telemetry**, now nearly free because the SDKs
surface compaction as first-class events instead of burying it in a log. #01 (Copilot) and #04 (Codex)
each already expose the event at their session seam; this ticket carries that value the rest of the
way — persisted per sub-issue to **both stores** and surfaced in the run report.

This **extends the persistence path Phase A built** for consumption; it does not invent a parallel
one. Follow that path exactly: the same per-sub-issue record, the same two stores, the same run-report
surface that already carries `consumed_tokens`. Add the compaction field alongside it.

## Acceptance criteria

- [x] A per-sub-issue auto-compaction figure, sourced from the SDK events exposed by #01 and #04, is
      persisted to both stores alongside `consumed_tokens` — reusing the Phase A persistence path, not
      a new one.
- [x] The run-end report surfaces auto-compaction per sub-issue next to consumption.
- [x] A test drives a stub session that emits compaction event(s) and asserts the figure is persisted
      to both stores and appears in the report.
- [x] No test invokes a real SDK session or a live model.
- [x] `uv run pytest -q`, `uv run mypy`, `uv run ruff check` all green.

## Blocked by

- #01 — the Copilot SDK session that surfaces compaction events.
- #04 — the Codex decision, which lands Codex's compaction telemetry at its seam.
