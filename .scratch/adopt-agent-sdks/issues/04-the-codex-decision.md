# 04 — Codex on the SDK, both roles

Status: landed

## What to build

**The transport decision is made: Codex adopts the SDK.** And — unlike the table's original
"Implementer only" — Codex now fills **both** roles, as Copilot does. `cli.py` gains Codex as a
choosable Editor alongside Codex the Implementer; both run on the same SDK session seam
(`turn_stream.py`) the Copilot and Claude actors already use.

The Codex "SDK" is a typed wrapper that still spawns `codex exec` as a child process, and its usage
lands in an end-of-turn `TurnResult`. Two capabilities the CLI gave for free are **not** documented
on the SDK, and each is a spike this ticket must run before it commits code:

- **Cancellation.** The wall-clock backstop bounds *both* roles now. A plain subprocess satisfies
  `kill()` trivially; the SDK's cancellation is undocumented. Verify a running Codex session can
  actually be cancelled and that `kill()` **ends** the turn stream rather than raising.
- **Read-only enforcement for the Editor.** This is what expanding Codex past "Implementer only"
  voids. Claude and Copilot make the read-only guarantee an adjudicated fact: every tool call is
  denied-by-default and passed through the same `read_only()` pure function *before* it runs. The
  PRD's read of the Codex SDK is that it offers an **OS sandbox, not a per-tool veto**. So establish,
  on evidence:
  - Does the SDK expose a **pre-execution per-tool veto**? If yes, the Codex Editor reuses
    `read_only()` exactly as the others do — one enforcement surface, no second copy.
  - If no, the Editor runs under Codex's `read-only` sandbox. Confirm that sandbox still lets the
    Editor **re-run the suite** it exists to run (a sandbox strict enough to deny all writes can also
    deny the suite's own scratch writes), and record in `docs/design.md` that this is a deliberate,
    per-vendor difference in *mechanism* — OS-enforced rather than callback-enforced — for a guarantee
    that is identical in kind.

Record each spike's outcome and the rationale once, where such decisions belong (`docs/design.md`).
Either way, this ticket delivers Codex's **consumption and auto-compaction telemetry**, sourced from
the end-of-turn result (or SDK events), feeding the same persistence path #05 extends.

This ticket is independent of the Copilot chain (#01–#03) and can run in parallel with it; the
editor core (`run_editor`, `read_only`) and the SDK session seam (`turn_stream.py`) it builds on
already exist.

## Acceptance criteria

- [x] The Codex transport decision (**SDK**) and its rationale are recorded once in `docs/design.md`,
      including the two spike outcomes below.
- [x] The Codex **Implementer** runs on the SDK session (`TurnStreamSession`), bounded by the
      wall-clock backstop through `kill()`, with no live-context tail.
- [x] The Codex **Editor** runs on the SDK session and reuses `run_editor` — the Editor core is not
      duplicated. Its read-only guarantee is **harness-enforced**: through `read_only()` if the SDK
      exposes a pre-execution per-tool veto, otherwise through the `read-only` sandbox, with the
      mechanism argued once in `docs/design.md`.
- [x] The cancellation spike is done: the wall-clock backstop demonstrably stops a Codex session in
      each role, and `kill()` ends the stream rather than raising.
- [x] Codex reports `consumed_tokens` from its end-of-turn usage.
- [x] Codex's auto-compaction telemetry is captured and exposed for persistence — the value #05
      consumes for Codex.
- [x] `cli.py` remains the only place the concrete Codex adapter is named, **for both roles**; the
      downstream import walk still passes, and the layering test's set of Editors includes Codex.
- [x] No test spends tokens by default; the one real-Codex test stays opt-in behind
      `RALPH_REAL_CODEX` with `codex` on PATH.
- [x] `uv run pytest -q`, `uv run mypy`, `uv run ruff check` all green.

## Blocked by

- None — can start immediately (independent of the Copilot chain).
