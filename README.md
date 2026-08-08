# Ralph Loop

Harness engineering for autonomous issue execution via coding agents.

Four actors. A **Planner** (human-invoked) cuts a parent issue into a graph of sub-issues. An
**Implementer** (Codex or Copilot) writes the code and the tests, one sub-issue per session, in
an isolated worktree. An **Editor** (Claude Code or Copilot, read-only) diagnoses the sessions
that fail and returns a verdict. An **Integrator** (Codex by default) reconciles the conflicts
that parallel landing creates.

The harness is the machinery between them: it dispatches sub-issues as their dependencies land,
bounds every session, classifies every failure honestly, and lands work through a lock-guarded
**merge gate** that merges the integration branch in, re-runs the suite on the result, and
fast-forwards — so the integration branch is correct by construction.

> **Status: it runs.** All eleven sub-issues in `.scratch/build_harness/` have landed. What has
> **not** happened: no run has yet been driven end-to-end by a real model. Every test in the suite
> uses a scripted stand-in agent — a real subprocess doing real git work, with no intelligence in it
> — which is what makes the harness testable at all, and is also exactly the gap that remains.

## Setup

The project is managed with [uv](https://docs.astral.sh/uv/). One command, from a fresh clone:

```bash
uv sync                        # creates .venv on the right Python, from uv.lock
uv sync --extra editor         # ...and the Claude Agent SDK, if you use --editor claude
```

uv fetches the interpreter itself — `.python-version` pins the project to 3.12, the floor of
`requires-python`, so a 3.13-only feature fails here rather than in the checkout of someone we
promised 3.12 to.

There is nothing to activate: `uv run <cmd>` syncs and runs in one step.

```bash
uv run pytest -q               # the suite
uv run mypy                    # strict, over ralph/ and tests/
uv run ruff check
```

## Usage

```bash
uv run ralph validate <repo> [issue-source]  # refuses a run this repo is not ready for
uv run ralph run --dry-run <repo>            # the build order, without opening a session
uv run ralph run <repo> [issue-source]       # run the graph to completion
```

`validate` refuses; it does not warn. A protected branch, a dirty tree, a missing test command, a
pre-commit hook the repo asks for and has not installed, a graph that will not parse, an actor whose
CLI or SDK is not installed — each gets its own sentence, and `ralph run` runs the same checks before
it dispatches anything.

A run narrates itself while it goes: every event appended to `<repo>/.scratch/<phase>/run.jsonl` is
printed as it is written, so a run in flight is watchable rather than silent until its notification.
The lines stay skimmable because the bodies are kept elsewhere — each session's full output goes to
`<repo>/.scratch/<phase>/transcripts/<sub-issue>/<cycle>-<actor>.log`.

```
16:42:22  VIR-80     implementer  session-started   in-progress
16:54:02  VIR-82     implementer  session-finished  integration-failed
16:55:19  VIR-80     implementer  sub-issue-closed  landed
17:12:44  VIR-82     editor       verdict-recorded  revise
```

## Target repo commands

Each target repo declares Ralph's commands in a `.ralph.toml` file at its root:

```toml
[commands]
test = "uv run pytest -q"  # required
install = "uv sync"        # optional
```

`test` is the command the merge gate runs on the prospective merge. `install`, when present, runs
once in the base checkout before any worktree is opened. Both command strings are split with shell
quoting rules.

A repo with no discoverable `test` command is not ready to run. `ralph validate` prints the
discovered commands when readiness passes, and refuses when the descriptor is missing, malformed, or
does not declare a runnable `test`.

## Reading order

| Document | What it is |
|---|---|
| `docs/design.md` | Why the system is shaped this way, and the bets taken deliberately |
| `docs/architecture.md` | The map — layers, where each concept lives, seams, invariants |
| `docs/cli-metering.md` | How each backend CLI exposes token-consumption telemetry |
| `docs/harness-flow.mmd` | The control flow of one run, as a diagram |
| `UBIQUITOUS_LANGUAGE.md` | Canonical for every domain term, **including code identifiers** |
| `CLAUDE.md` | How to work in this repo (status, commands); coding rules auto-load from `.claude/rules/` |

Documents reference each other in one direction only — from orientation toward detail — so there is
always a valid reading order and no reference cycles. `README.md` and `CLAUDE.md` are the two entry
points (human and agent); `UBIQUITOUS_LANGUAGE.md` (terms) and `docs/design.md` (why) are the shared
leaves everything else points down to.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — it fetches Python 3.12 itself,
  so that is the only thing you must install first
- `codex` or `copilot` CLI for the Implementer and the Integrator; Claude Code or `copilot`
  for the Editor
- Git 2.38+ (worktree support)
- [mattpocock/skills](https://github.com/mattpocock/skills) at user level (provides `/tdd`):
  ```bash
  npx skills@latest add mattpocock/skills
  ```

## Issue format

Sub-issues are Markdown files in the target repo's `.scratch/<phase>/issues/` directory:

```markdown
# 01 — Add user authentication

Status: ready

Short description of the work.

## Acceptance criteria
- [ ] Users can sign in with email/password
- [ ] Invalid credentials return a 401

## Blocked by
- #00 — database schema
```

**Status values:** `ready` → `in-progress` → `landed`, or `needs-human`.

Four, and no others. `ready` is the Planner's authorisation to run — a sub-issue it has not
authorised does not belong in the graph yet, so there is no `not-started`.

`landed` is a sub-issue's terminal state; `done` belongs to the parent issue and is never written
to a sub-issue. The merge gate sets `landed` automatically, after the fast-forward. Do not set it
by hand.

**Dependencies:** list blockers under `## Blocked by` using `#N` references (matched to `N-*.md`)
or bare filenames. A sub-issue becomes eligible only once every sub-issue it is blocked by has
`landed`. `Blocked` refers to this edge and nothing else.

**PRD:** place a `PRD.md` one level above the `issues/` directory. Ralph does not inject it into
any prompt; it is discoverable directly, since Implementer and Editor sessions read the full repo
checkout, PRD included.

## Linear issue source

Filesystem issues remain discoverable when `issue_source` is omitted. To run from Linear, pass
`--issue-mode linear`, pass the parent issue identifier as `issue_source`, and set `LINEAR_API_KEY`:

```bash
LINEAR_API_KEY=lin_api_... uv run ralph run --dry-run --issue-mode linear <repo> ENG-123
```

The Linear parent issue's sub-issues are Ralph's sub-issues. Linear's native issue relation
`blocked by` supplies graph edges. Each sub-issue description stores the current Ralph content:

```markdown
## Spec

The current spec, including acceptance criteria.

## Findings

The current findings, if any.
```

When the Editor records a revision, Ralph snapshots the original description as `Ralph revision 0`
in a Linear comment, updates the sub-issue description to the latest spec/findings, then appends
the new revision as another Ralph comment. The description stays readable; the revision trail stays
attached to the sub-issue.

## Failure taxonomy

Three failure outcomes, and each one routes somewhere specific — `integration-failed` by two paths,
because a conflict and a red suite are different problems:

| Outcome | Meaning | Goes to |
|---|---|---|
| `impasse` | The Implementer did not deliver — it said why, or committed nothing | Editor |
| `integration-failed` | Committed work is **red** on the prospective merge | Editor |
| `integration-failed` | Committed work **conflicts** on the prospective merge | Integrator |
| `infra-failed` | The environment is broken, not the code | Human |

**There is no retry destination in this system.** A failed sub-issue is quarantined — marked
`needs-human`, worktree preserved, its dependents never become eligible — and everything
unaffected still lands. The human is paged **once**, at the end. The run never stops early.

A **merge conflict** is the one failure answered inside the merge gate rather than by the Editor.
The gate keeps its lock and dispatches an
**Integrator** to resolve the conflict and commit it. The result goes through the same suite gate as
any other landing, and the sub-issue never returns to the gate: it lands, or it goes to the human.

## Run Options

A run reads every non-secret argument from the CLI and the one secret from `.env`. Ralph loads the
whole `.env` so its suite inherits the target repo's own variables (`DATABASE_URL`, …) too, and
reads only `LINEAR_API_KEY` for itself.

The CLI defaults match Ralph's current common path:

```bash
--issue-mode filesystem
--implementer codex
--editor claude
--integrator codex
--protected main
--protected master
```

How `codex`/`copilot` is driven, and the four Linear state names, are hardcoded in the adapter that
owns them.

`--sequential` runs one sub-issue at a time instead of every eligible one at once. It narrows what
the scheduler dispatches and nothing else: the merge gate, the suite gate, and the fast-forward are
the same on either setting, so the run is slower and strictly no safer. Reach for it when the
constraint is outside the harness — a rate limit, a machine that cannot host N checkouts, or a
session you want to watch one at a time.

### `.env` — the one secret

| Variable | Description |
|---|---|
| `LINEAR_API_KEY` | Linear API key. Required only when `issue_mode: linear`. |

Operational verbosity is the `--log-level` flag; `issue_source` is the per-run positional argument.
Neither is a secret, so neither lives here.

## Design principles

1. **Target repos stay agnostic** — Ralph never modifies target repo structure. It reads
   `.scratch/` for issues, `.ralph.toml` for commands, a repo-level agent context file
   (`CLAUDE.md` for Copilot, `AGENTS.md` or `CLAUDE.md` for Codex), and `.env` at the repo root.
2. **Single-repo scope** — intra-repo dependencies only. Cross-repo sequencing is the user's.
3. **Skills as references** — the prompt invokes `/tdd` by name. Skills are installed at user
   level, never bundled here.
4. **Worktree isolation** — every session runs in its own worktree. Parallel sessions land one at
   a time, through the merge gate.
5. **The merge gate runs the harness's only suite gate.** A model's exit code is its opinion; the suite is a
   fact only when Ralph runs it on the prospective merge. A suite the harness runs is still inside
   the **blast radius** — only CI on a clean checkout is **honest**.
