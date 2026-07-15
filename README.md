# Ralph Loop

Harness engineering for autonomous issue execution via coding agents.

Three actors. A **Planner** (human-invoked) cuts a parent issue into a graph of sub-issues. An
**Implementer** (Codex or Copilot) writes the code and the tests, one sub-issue per session, in
an isolated worktree. An **Editor** (Claude Code or Copilot, read-only) diagnoses the sessions
that fail and returns a verdict.

The harness is the machinery between them: it dispatches sub-issues as their dependencies land,
bounds every session, classifies every failure honestly, and lands work through a lock-guarded
**merge queue** that rebases, re-runs the suite on the prospective merge, and fast-forwards — so
the integration branch is correct by construction.

> **Status: it runs.** All eleven sub-issues in `.scratch/build_harness/` have landed. What has
> **not** happened: no run has yet been driven end-to-end by a real model. Every test in the suite
> uses a scripted stand-in agent — a real subprocess doing real git work, with no intelligence in it
> — which is what makes the harness testable at all, and is also exactly the gap that remains.

## Setup

The project is managed with [uv](https://docs.astral.sh/uv/). One command, from a fresh clone:

```bash
uv sync                        # creates .venv on the right Python, from uv.lock
uv sync --extra editor         # ...and the Claude Agent SDK, if you want RALPH_EDITOR=claude
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
uv run ralph validate <repo> [issues-dir]    # refuses a run this repo is not ready for
uv run ralph run --dry-run <repo>            # the build order, without opening a session
uv run ralph run [-j N] <repo> [issues-dir]  # run the graph to completion
```

`validate` refuses; it does not warn. A protected branch, a dirty tree, a suite it cannot find, a
pre-commit hook the repo asks for and has not installed, a graph that will not parse — each gets its
own sentence, and `ralph run` runs the same checks before it dispatches anything.

## Reading order

| Document | What it is |
|---|---|
| `docs/design.md` | Why the system is shaped this way, and the bets taken deliberately |
| `docs/architecture.md` | The map — layers, where each concept lives, seams, invariants |
| `docs/cli-metering.md` | How each backend CLI exposes its context signal (the ceiling's input) |
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
- `codex` or `copilot` CLI for the Implementer; Claude Code or `copilot` for the Editor
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

Brief description of the work.

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
to a sub-issue. The merge queue sets `landed` automatically, after the fast-forward. Do not set it
by hand.

**Dependencies:** list blockers under `## Blocked by` using `#N` references (matched to `N-*.md`)
or bare filenames. A sub-issue becomes eligible only once every sub-issue it is blocked by has
`landed`. `Blocked` refers to this edge and nothing else.

**PRD:** place a `PRD.md` one level above the `issues/` directory. It is injected into every
session as design context.

## Failure taxonomy

Four outcomes, and each one routes somewhere specific:

| Outcome | Meaning | Goes to |
|---|---|---|
| `impasse` | The Implementer did not deliver — it said why, committed nothing, or left the suite red | Editor |
| `integration-failed` | Green alone, red or conflicting on the merge | Editor |
| `ceiling-exceeded` | The session left the model's smart zone | Human |
| `infra-failed` | The environment is broken, not the code | Human |

**There is no retry anywhere in this system.** A failed sub-issue is quarantined — marked
`needs-human`, worktree preserved, its dependents never become eligible — and everything
unaffected still lands. The human is paged **once**, at the end. The run never stops early.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `RALPH_IMPLEMENTER` | *unset* | `codex` or `copilot`. **Unset is not a default** — it means the argv in `RALPH_AGENT_CMD`, bounded on the clock alone |
| `RALPH_EDITOR` | *unset* | `claude` or `copilot`. **Unset means there is no Editor in this run** — quarantine-and-drain, failures escalating on the Implementer's own outcome |
| `RALPH_AGENT_CMD` | — | The Implementer's argv when `RALPH_IMPLEMENTER` is unset. `{sub_issue}` is substituted |
| `RALPH_TEST_CMD` | autodetected | Overrides suite detection. A repo with no detectable suite is refused |
| `RALPH_INSTALL_CMD` | autodetected | Runs **once**, in the base checkout. A failure aborts the run |
| `RALPH_PROTECTED_BRANCHES` | `main master` | Branches Ralph refuses to run on |
| `CODEX_MODEL` | `gpt-5.3-codex` | Model passed to `codex exec --model` |
| `CODEX_SANDBOX` | `workspace-write` | Sandbox passed to `codex exec --sandbox` |
| `CODEX_APPROVAL` | `never` | Approval policy passed to `codex exec --ask-for-approval` |
| `RALPH_CODEX_UNSANDBOXED` | `0` | `1` bypasses Codex approvals and sandbox |
| `COPILOT_MODEL` | `gpt-5.3-codex` | Model passed to `copilot --model` |

## Design principles

1. **Target repos stay agnostic** — Ralph never modifies target repo structure. It reads
   `.scratch/` for issues and a repo-level agent context file (`CLAUDE.md` for Copilot,
   `AGENTS.md` or `CLAUDE.md` for Codex).
2. **Single-repo scope** — intra-repo dependencies only. Cross-repo sequencing is the user's.
3. **Skills as references** — the prompt invokes `/tdd` by name. Skills are installed at user
   level, never bundled here.
4. **Worktree isolation** — every session runs in its own worktree. Parallel sessions land one at
   a time, through the merge queue.
5. **The suite result, not the exit code, is the outcome.** A model's exit code is its opinion;
   the suite is a fact. And a suite the harness runs is inside the **blast radius** — only CI on a
   clean checkout is **honest**.
