# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Ralph is a **harness** for autonomous issue execution. Four actors: a **Planner** (human) cuts a
parent issue into a graph of sub-issues; an **Implementer** (Codex or Copilot) writes code + tests,
one sub-issue per session in an isolated worktree; an **Editor** (Claude Code or Copilot, read-only)
diagnoses failed sessions and returns a verdict; an **Integrator** (Codex by default) reconciles the
merge conflicts that parallel landing creates. The harness is the machinery between them.

## Status

The harness runs — all eleven build sub-issues in `.scratch/build_harness/` have landed. What has
**not** happened: no run driven end-to-end by a real model. Every test uses a scripted stand-in agent
(a real subprocess doing real git work, no intelligence), which is what makes the harness testable —
and is exactly the gap that remains.

## Commands

**Use `uv run`, never a bare `python3`.** System `python3` is 3.10 and lacks `StrEnum`, so it cannot
even import `ralph.harness`. `uv run` resolves the interpreter from `requires-python` and syncs against
`uv.lock` before running — no venv to activate, no drifted dependency to debug.

```bash
uv sync                         # set up .venv from uv.lock
uv sync --extra editor          # + Claude Agent SDK, for --editor claude
uv run pytest -q                # the suite — green before every commit
uv run pytest tests/harness/test_scheduler.py::test_name   # a single test
uv run mypy                     # strict; covers ralph/ AND tests/
uv run ruff check
uv run ralph …                  # the CLI
```

- **`uv.lock` is the single source of truth for the environment.** Add deps with `uv add`
  (`--group dev` for tooling), never by hand-editing `pyproject.toml`.
- **`tests/` is inside mypy's scope on purpose:** the assertion that each fake satisfies its Protocol
  is a type annotation only mypy checks — runtime `isinstance` on a Protocol compares method names,
  not signatures, and would wave a broken fake through.
- **Real-model tests are skipped by default.** The one Codex test that spends tokens un-skips only
  with `RALPH_REAL_CODEX=1` and `codex` on PATH.

The CLI:

```bash
uv run ralph validate <repo> [issue-source]  # refuses a run this repo is not ready for
uv run ralph run --dry-run <repo>            # the build order, without opening a session
uv run ralph run <repo> [issue-source]       # run the graph to completion
```

## Architecture

[`docs/architecture.md`](docs/architecture.md) is the map — read it before changing code. The
load-bearing facts:

- **One process, asyncio.** A run is a single process; the merge lock is an `asyncio.Lock`. No
  `flock`, no PID files, no result-file polling. Reaching for any of those means you mistranslated the
  design.
- **Layers, dependency arrow inward:** `harness/` → `ports.py` → `adapters/` → orchestration →
  `cli.py`. `harness/` is pure (stdlib only, no I/O). `harness/model/` is the nouns (frozen values,
  zero logic); `harness/rules/` is the verbs (the pure functions that *are* the design). Every
  `ports.py` Protocol has a fake in `tests/fakes.py`, and the fakes are what the suite runs against.
- **Concrete adapters are named only in `cli.py`.** Nothing downstream knows whether the Implementer
  is Codex or Copilot, the Editor is Claude Code or Copilot, or the Integrator is either. Any of
  these CLIs can back any of the three unattended actors.
- **The merge gate runs the harness's only suite gate.** The harness runs the discovered test command on the
  prospective merge; a model's exit code is only its opinion. A suite the harness runs is inside the
  **blast radius** — only CI on a clean checkout is **honest**.
- **No retry destination.** A failed sub-issue is quarantined (`needs-human`, worktree preserved, its
  dependents never become eligible); everything unaffected still lands. The run never stops early;
  the human is paged **once**, at the end.
- **A conflict is answered in the gate, not by the Editor.** On a merge conflict the merge gate
  holds its lock, merges the integration branch into the worktree, and dispatches an **Integrator**
  to resolve and commit. The sub-issue does not re-enter the gate: it either passes the suite gate
  and lands, or it goes to the human. This is not a retry — it is a different actor answering a
  question the Implementer could not have seen.
- **The Editor writes nothing but spec and findings,** enforced by a tool allowlist (not the
  prompt): `adapters/runtime/editor.py` for the model-agnostic half. At most **three cycles**, enforced by the
  scheduler alone.

## Docs and vocabulary

- **Coding rules auto-load** from [`.claude/rules/`](.claude/rules/) (`coding-standards.md`,
  `canonical-vocabulary.md`) — every session, no import needed. Do not duplicate them here.
  `coding-standards.md` is deliberately repo-agnostic; the **one Ralph adaptation** lives here: its
  *"if something is unclear, ask"* assumes a human in the session, and an unattended Implementer has
  none — its way of asking is **`<impasse>`**, with a structured report. Guessing, or softening an
  acceptance criterion until it passes, is the failure mode the whole harness exists to catch.
- [`UBIQUITOUS_LANGUAGE.md`](UBIQUITOUS_LANGUAGE.md) is canonical for every domain term, **including
  code identifiers**. An "aliases to avoid" word as a class/function/field/state name is a defect.
- **Why** the system is shaped this way: [`docs/design.md`](docs/design.md). **Per-CLI token
  consumption telemetry**: [`docs/cli-metering.md`](docs/cli-metering.md).
- The docs form an acyclic reference DAG (README + CLAUDE are roots; UL + design.md are leaves). When
  editing, point *down* toward detail — never add a reference from a leaf back up.

## Working in a target repo

- Sub-issues are Markdown in `<repo>/.scratch/<phase>/issues/*.md`, matched by numeric prefix.
  `Status:` values are exactly `ready` → `in-progress` → `landed` | `needs-human` — anything else is
  a loud, fatal parse error. `landed` is set by the merge gate; never by hand. A `PRD.md` one level
  above `issues/` is not injected into any prompt; it is discoverable directly, since sessions read
  the full repo checkout.
- Worktrees live at `<repo>/.worktrees/active/`; failures preserved at `<repo>/.worktrees/failed/`.
- A run writes two artifacts under `<repo>/.scratch/<phase>/`: `run.jsonl`, the authoritative
  ordered record and nothing heavier, and `transcripts/<sub-issue>/<cycle>-<actor>.log`, one file
  per session holding everything it said. Nothing reads the transcripts back.
- Target repos stay agnostic — Ralph reads `.scratch/`, a repo-level context file (`CLAUDE.md` for
  Copilot; `AGENTS.md` then `CLAUDE.md` for Codex), and `.env` at the repo root, and modifies
  nothing else.
- A run's non-secret arguments come from CLI flags. `.env` carries the one secret,
  `LINEAR_API_KEY`. The argument list and the failure taxonomy are in [`README.md`](README.md).
