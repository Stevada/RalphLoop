# Ralph Loop — Harness Engineering

## Status: the harness is being built

The bash prototype has been removed. The Python harness is being built now, by Claude Code
working directly in this repo — **not** by the harness's own Implementer, which does not exist
yet. The build order is `.scratch/build_harness/` (parent PRD + eleven sub-issues); the contract
is `docs/architecture.md`.

Do not reference `src/*.sh` — it is gone.

## Commands

**Use `.venv/bin/python`, not `python3`.** The system `python3` is 3.10 and has no `StrEnum`, so
it cannot even import `ralph.domain`. The venv is 3.12, created with `uv venv --python 3.12 &&
uv pip install -e ".[dev]"`.

- Test: `.venv/bin/python -m pytest -q` — must be green before every commit
- Typecheck: `.venv/bin/python -m mypy` (strict; covers `ralph/` **and** `tests/`)
- Lint: `.venv/bin/python -m ruff check`

`tests/` is inside mypy's scope on purpose: the assertion that each fake satisfies its Protocol is
a type annotation, and only mypy checks it. Runtime `isinstance` on a Protocol compares method
names, not signatures, and would wave a broken fake through.

**`ralph run [-j N] <repo> [issues-dir]` works** — it reads the graph, refuses a red base, and runs
every eligible sub-issue **concurrently** (default 4, `-j` to change it), landing them **one at a
time** through the merge lock. A failure is classified, quarantined, and drained around: the run
does not stop, its dependents never get a turn, and one notification comes out at the end.

**The Editor loop is closed.** A failure that routes to the Editor gets a verdict, and on `revise`
the sub-issue **restarts clean** against a rewritten brief — the worktree and its branch are
destroyed, and the next cycle is cut from the integration branch. Knowledge survives **only** through
the findings. At most **three cycles**, enforced by the scheduler alone: the adapters *tell* the
model it is the final cycle, and the scheduler **refuses** a `revise` that comes back anyway.

There is **no Editor adapter yet** — `Scheduler(editor=None)` is the default, and it means *there is
no Editor in this run*, not a null one. The run is then quarantine-and-drain and failures escalate on
the Implementer's own outcome. #09 names the real one in `cli.py`.

The agent it runs is whatever `RALPH_AGENT_CMD` names; Codex and Copilot become two more of those
in #08 and #10.

Still to come: the context ceiling (#08), the real Editor (#09), `ralph validate` (#11).

## Issue format
- Issues live in `.scratch/<phase>/issues/` inside the target repo (e.g. `.scratch/refine_data_flow/issues/`)
- The `issues/` directory under `.scratch/` is auto-discovered when no explicit path is given
- Required: `Status:` line — values: `ready`, `in-progress`, `landed`, `needs-human`
  - These four are the whole set. `SubIssueState` has exactly these members, and a `Status:` line
    carrying anything else is a loud, fatal parse error — never a silent default.
  - `ready` is the Planner's authorisation to run. There is no `not-started`: a sub-issue the
    Planner has not authorised does not belong in the graph yet.
  - `landed` is a sub-issue's terminal state. `done` belongs to the parent issue and is never
    written to a sub-issue — see `UBIQUITOUS_LANGUAGE.md`.
- Required for agent context: `## Acceptance criteria` section
- Optional dependencies: `## Blocked by` section with bullet lines — each non-None bullet is matched against sibling issue filenames by numeric prefix (e.g. `#02`, `PDA #02`, `02-remove-payload.md` all match `02-*.md`)
- The merge queue sets `Status: landed` automatically after a successful land; do not set it manually

## Target repo requirements
- Copilot runs require `CLAUDE.md` at root; Codex runs prefer `AGENTS.md` and fall back to `CLAUDE.md`
- Must be on a non-protected branch before running (default protected: `main`, `master`)
- Worktrees are created at `<repo>/.worktrees/active/` and failures preserved at `<repo>/.worktrees/failed/`

## PRD support
Place a `PRD.md` one level above the `issues/` dir (i.e. `.scratch/<phase>/PRD.md`). It is injected into every session as design context.

## Environment variables
- `COPILOT_MODEL` — model passed to `copilot --model` (default: `gpt-5.3-codex`)
- `CODEX_MODEL` — model passed to `codex exec --model` (default: `gpt-5.3-codex`)
- `CODEX_SANDBOX` — sandbox passed to `codex exec --sandbox` (default: `workspace-write`)
- `CODEX_APPROVAL` — approval policy passed to `codex exec --ask-for-approval` (default: `never`)
- `RALPH_IMPLEMENTER` — `codex` or `copilot` (named only in `cli.py`)
- `RALPH_EDITOR` — `claude` or `copilot` (named only in `cli.py`)
- `RALPH_AGENT_CMD` — the Implementer's argv. `{sub_issue}` is substituted with the sub-issue's id,
  which the harness also puts on the worktree's branch. An Implementer, at this layer, *is* an argv.
- `RALPH_TEST_CMD` — overrides suite autodetection (npm, pytest, make); a repo with neither is a
  loud, fatal error, never a green `SuiteResult`
- `RALPH_INSTALL_CMD` — overrides install autodetection (`npm ci` when there is a `package.json`).
  Runs **once**, in the base checkout, never per worktree. A failure aborts the run — never `|| true`
- `RALPH_CODEX_UNSANDBOXED` — set to `1` to pass `--dangerously-bypass-approvals-and-sandbox` to Codex
- `RALPH_PROTECTED_BRANCHES` — space-separated list of branches to refuse running on (default: `main master`)

## Coding Rules

**Behavioural rules: `docs/karpathy_CLAUDE.md`.** How to work — think before coding, simplicity
first, surgical changes, goal-driven execution. The rules below are what to build; that file is
how to go about it. Both bind.

One adaptation, and it matters: *"if something is unclear, ask"* assumes a human in the session.
An unattended Implementer has none — its way of asking is **`<impasse>`**, with a structured
report. Guessing, or softening an acceptance criterion until it passes, is the failure mode the
whole harness exists to catch.

### 1. Type Discipline

- Avoid `Any` — define explicit types, Protocols, or TypedDicts for all function signatures, return values, and module-level variables.
- When wrapping third-party SDKs that return untyped objects, define a local Protocol or TypedDict describing the shape we actually use.
- `Any` is acceptable only at import-fallback boundaries (`except ImportError` guards) — mark those with `# type: ignore` and a comment explaining why.

### 2. DRY — Don't Repeat Yourself

- Every piece of knowledge must have a single, unambiguous, authoritative representation in the codebase.
- If you find yourself copying logic, extract it into a shared function, class, or module.
- Configuration values, magic strings, and constants must be defined once and imported — never duplicated across files.

### 3. Single Source of Truth

- Each concept (schema, config shape, domain type, business rule) is owned by exactly one module.
- Other modules import from the owner — they never redefine or shadow it.
- When a third-party type needs adaptation, create one wrapper/protocol in one place; consumers depend on that wrapper.
- If two modules need the same data shape, move it to a shared location rather than defining it twice.

### 4. Canonical Vocabulary

`UBIQUITOUS_LANGUAGE.md` is canonical for every domain term, **including code identifiers**.
A word from its "Aliases to avoid" column appearing as a class, function, field, variable, or
state name is a defect, not a style nit.

- A sub-issue's terminal state is `landed`. `done` belongs to the parent issue.
- The mutex is the **merge lock**, never the "integration lock" or "queue lock".
- **Sub-issue**, never task/ticket/issue. **Findings**, never notes/hints/guidance.
- `SuiteResult.green` — never `verified`, `trusted`, or `passing`. A suite the harness runs
  is inside the **blast radius**; only CI on a clean checkout is **honest**. Holding the line
  in the identifiers is what stops the distinction eroding.
- **Blocked** refers only to Linear's `blocked by` edge. It never describes a session's state.

### 5. Architecture Conventions

`docs/architecture.md` is the contract. Layers, and the dependency arrow points inward:
`domain/` → `ports.py` → `adapters/` → orchestration → `cli.py`.

- **`domain/` is pure.** Stdlib imports only. No I/O, no subprocess, no git, no model. If a
  domain function needs a fact from the world, it takes it as an argument.
- **`ralph/domain/__init__.py` is the domain's interface.** Import `from ralph.domain import
  Outcome`, never `from ralph.domain.model.session import Outcome`. The layout inside is an
  implementation detail; callers should not have to learn it.
- **`domain/model/` is the nouns; `domain/rules/` is the verbs.** `model/` holds frozen values
  with zero logic. `rules/` holds the pure functions that *are* the design — the failure taxonomy,
  the routing table, eligibility, the cycle cap. **`rules/` may import `model/`; `model/` may not
  import `rules/`** — a test enforces it. A value that knows how it will be classified has stopped
  being a value. New decision logic goes in `rules/`, never beside the type it decides about.
- **The domain is not split by actor, and `adapters/` is not split by port.** `Outcome` and
  `SessionTelemetry` belong to both actors; `copilot.py` is both an Implementer and an Editor.
  Grouping either way forces a `shared/` folder that swallows everything.
- **The fakes live in `tests/fakes.py`, never in `ralph/`.** Nothing in the shipped package may
  import from `tests/` — a test asserts it. An adapter that reaches for a fake has stopped being
  an adapter. `tests/builders.py` is a separate thing: builders make *values*, fakes satisfy
  *Protocols*.
- **Structure is a frozen value; content is state.** `@dataclass(frozen=True, slots=True)`
  for domain types. `IssueGraph` and `SubIssue` are immutable for a run's whole life — the
  invariant "the Editor may never re-link a sub-issue" is enforced by the type, not by a rule
  someone must remember.
- **Enums, not strings.** `Outcome`, `Verdict`, `SubIssueState`, `Destination` are `StrEnum`.
  A raw outcome string anywhere outside a parser is a defect.
- **Every Protocol in `ports.py` has a fake**, and the fakes are what the test suite runs
  against. A test that needs a real model, a real network, or a real `codex` binary is in the
  wrong layer.
- **One process, asyncio.** The merge lock is an `asyncio.Lock`. No `flock`, no PID files,
  no polling for result files.
- **Concrete adapters are named in `cli.py` and nowhere else.** Nothing downstream knows
  whether the Implementer is Codex or Copilot, or the Editor is Claude Code or Copilot.

### 6. Failure Discipline

The harness's whole value is that it classifies failure honestly. Code that blurs a failure
is worse than code that has one.

- **Fail fast, loudly.** Raise on missing required fields — no silent defaults. Never
  `|| true`, never a bare `except:`, never swallow a subprocess's exit code.
- **Zero commits is never a benign skip.** It is `silent-red` or `infra-failed`.
- **The suite result, not the exit code, is the outcome.** The harness runs the tests. A
  model's exit code is its opinion; the suite is a fact.
- **No string-keyed intermediates.** Typed records throughout; no `dict[str, Any]` layers
  between a CLI's JSON and a domain type — parse at the boundary, into a dataclass.

