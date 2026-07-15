# CLI metering — how each backend exposes context

Reference for the one fact the **context ceiling** depends on: the number of tokens in a model's
context on its most recent call, read **live**, while the session runs. Every fact here was
confirmed by running a real session and reading what came out. This document owns the per-CLI
empirics behind the ceiling — external to our code, hard-won, and looked up on their own; the harness
seam that *consumes* these signals lives in the adapters.

The short version of *why* the ceiling is on context and not consumption, because it governs
everything below:

> The ceiling is on **context** (the model's smart zone), never **consumption** (what the session
> spent). Both numbers arrive in the **same** event, adjacent, with names that read alike. A ceiling
> wired to the wrong one is not merely inaccurate — it is **inverted**: it kills a long, cheap,
> tightly-focused session and waves through a bloated one.

## Neither CLI's obvious channel is the right one

| CLI | The obvious channel | Why it is useless to the ceiling |
|---|---|---|
| **Codex** | `codex exec --json` stdout | `turn.completed` (carrying `usage`) lands only at session **end** — one `exec` is a single turn — when the ceiling has nothing left to prevent. |
| **Copilot** | `--output-format json` stream | Publishes only `outputTokens`, the **completion** count. Never mentions the prompt. Precisely the number the ceiling does not want. |

In both cases the number the ceiling needs is written to a **file on disk, live**, and in neither
case is it on stdout.

| | Context signal | Source | Baseline context |
|---|---|---|---|
| **Codex** | `info.last_token_usage.input_tokens` | `~/.codex/sessions/…/rollout-*.jsonl` (JSONL) | ~16k |
| **Copilot** | `usage.prompt_tokens` | `--log-dir` log, `--log-level debug` (JSON blocks) | **56.5k**, cut to **8.4k** |

---

## Codex — `adapters/codex.py`

The session **rollout file** (`~/.codex/sessions/<date>/rollout-*.jsonl`) gets a `token_count`
event appended after **every model call**:

```json
{"type": "event_msg", "payload": {"type": "token_count", "info": {
  "last_token_usage":  {"input_tokens": 16802},   ← the context on the last call. THE CEILING.
  "total_token_usage": {"total_tokens": 33410},   ← consumption. telemetry only.
  "model_context_window": 272000
}, "rate_limits": {"primary": {"used_percent": 0.0}}}}
```

`CodexContextSource` tails the rollout file (not stdout) and yields `last_token_usage.input_tokens`.

Load-bearing details:

- **The `thread_id` handshake.** `thread.started` on stdout announces which rollout file belongs to
  this session. This is not a nicety: four Codex sessions run at once by default, each appending to
  its own rollout file in the same directory. Metering a sibling's file would kill the wrong session
  and report `ceiling-exceeded` against a sub-issue that never left the smart zone. Stdout is needed
  for exactly this one live fact — `Transcript` serves it while also accumulating the full session
  output for telemetry, and stops republishing once a `ContextSource` has found its handshake.
- **The ceiling (120k) sits far below the window (272k)**, so it always fires **before** Codex would
  auto-compact — compaction never gets to drop the context back under the bound and hide the
  crossing.
- `rate_limits.primary.used_percent` rides along in the same event: free early warning for the
  429 → `infra-failed` case. Log it; do not gate on it yet.

---

## Copilot — `adapters/copilot.py`

Launched:
`copilot -p <prompt> --model <m> --log-dir <fresh> --log-level debug --no-color
--disable-builtin-mcps --disable-mcp-server <each> --allow-all-tools`.

The context lives in the **debug log** and nowhere else:

```json
"usage": {
  "prompt_tokens": 25885,        ← the context on this call. THE CEILING.
  "completion_tokens": 4,
  "total_tokens":  25889,        ← what THIS CALL spent. NOT a running total.
  "prompt_tokens_details": { "cached_tokens": 0, "cache_creation_tokens": 25883 }
}
```

`CopilotContextSource` tails the log and yields `usage.prompt_tokens`. Four things differ from Codex,
and every one is a way the ceiling silently stops working:

- **`--log-level debug` is required.** At the default level there are no `usage` blocks at all. A
  session that publishes no observation is not cheap, it is **unmetered** — so the adapter *raises*
  rather than run it to the wall clock unwatched.
- **The log is pretty-printed JSON inside a timestamped text log, not JSONL.** It needs a
  brace-matched block extractor that counts braces **outside string literals only** — the blocks
  embed the whole prompt, and the prompt embeds the brief. One `if (x) {` in a target repo's
  acceptance criteria is an unbalanced brace inside a JSON string, and a naïve counter never finds
  the end of the block.
- **The block must be parsed before it is trusted.** The same log carries a model-capabilities block
  with `max_prompt_tokens: 200000`. Anything grepping for the *string* `prompt_tokens` would read the
  model's context **limit** as its **usage** and kill every Copilot session before its first turn.
  `usage` is read from the top level of the parsed object.
- **`total_tokens` is per-call, where Codex's `total_token_usage` is cumulative** (25,885 + 4 =
  25,889, and the next call starts over). The adapter accumulates, because `ContextMeter` takes a
  `max()` over what it is handed and would otherwise report the largest single call as the whole
  session's spend.

Unlike Codex, a Copilot session has **no thread id to announce and no shared directory**: it is handed
a private `--log-dir`, emptied first (a revised cycle re-cuts the same branch at the same path, so
last cycle's log would otherwise sit exactly where this cycle's is looked for). Nothing to
disambiguate, so no stdout handshake.

`--max-ai-credits` is a *credit* cap, not a context cap. It is not a substitute for the ceiling.

### MCP is disabled on every Copilot run

Copilot loads the world into the prompt before it reads the brief. Measured, one word in and one
word out:

| launched | context to answer "pong" | of the smart zone |
|---|---|---|
| default | **56.5k** | 47%, gone at turn zero |
| MCP disabled | **25.9k** | 22% |
| MCP disabled + read-only tool allowlist | **8.4k** | 7% |

So the harness disables MCP on every run — a ceiling that fires on work which never had room to begin
with is a tax, not a quality bound. **`--disable-builtin-mcps` is not enough on its own:** it disables
`github-mcp-server` and nothing else, while the servers actually costing the prompt come from the
user's config, the workspace, and installed plugins. The harness asks `copilot mcp list --json` and
disables each by name, because it cannot guess names it has never seen.

Codex, by comparison, starts at ~16k and needs none of this.
