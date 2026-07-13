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

> **Status: under construction.** A bash prototype proved the git plumbing and has been removed.
> The Python harness is being built now — see `.scratch/build_harness/` for the build order and
> `docs/architecture.md` for the contract. Nothing below is runnable yet.

## Reading order

| Document | What it is |
|---|---|
| `docs/prd.md` | Why the system is shaped this way, and the bets taken deliberately |
| `docs/architecture.md` | The contract — layers, types, Protocols, adapters |
| `docs/harness-flow.mmd` | The control flow of one run, as a diagram |
| `UBIQUITOUS_LANGUAGE.md` | Canonical for every domain term, **including code identifiers** |
| `CLAUDE.md` | Coding rules |

## Prerequisites

- Python 3.12+
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

Five outcomes, and each one routes somewhere specific:

| Outcome | Meaning | Goes to |
|---|---|---|
| `impasse` | The Implementer stopped and said why | Editor |
| `silent-red` | It claimed success; the suite disagrees | Editor |
| `integration-failed` | Green alone, red or conflicting on the merge | Editor |
| `ceiling-exceeded` | The session left the model's smart zone | Human |
| `infra-failed` | The environment is broken, not the code | Human |

**There is no retry anywhere in this system.** A failed sub-issue is quarantined — marked
`needs-human`, worktree preserved, its dependents never become eligible — and everything
unaffected still lands. The human is paged **once**, at the end. The run never stops early.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `RALPH_IMPLEMENTER` | `codex` | `codex` or `copilot`. Named only in `cli.py` |
| `RALPH_EDITOR` | `claude` | `claude` or `copilot`. Named only in `cli.py` |
| `RALPH_TEST_CMD` | autodetected | Overrides suite detection. Neither present is a fatal error |
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
