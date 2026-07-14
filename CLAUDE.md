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

**The Editor is real.** `RALPH_EDITOR=claude` runs Claude Code (Opus) via the Agent SDK against the
failed worktree, **read-only enforced by the harness** — an allowlist at `can_use_tool`, so a
mutating call is *denied*, not discouraged. It may re-run the repo's suite and grep the worktree, and
nothing else that executes: the Editor's value is **reproduction, not inference**, and declaring an
impasse is cheap enough that checking the story against the repository is the only thing standing
between us and a system where declaring an impasse always works.

`RALPH_EDITOR` unset means *there is no Editor in this run*, not a null one — the run is then
quarantine-and-drain and failures escalate on the Implementer's own outcome.

**The smart-zone ceiling is real, and Codex runs behind it.** `RALPH_IMPLEMENTER=codex` runs a real
`codex exec` session, metered on **context** — read live from the session's rollout file, identified
by the thread id Codex announces on stdout. A session that leaves the 120k smart zone is killed with
`ceiling-exceeded`, which is a human's problem (the sub-issue is too big), never `infra-failed`.

**The ceiling is on context, not consumption.** Cost is not the argument; quality is. The two numbers
arrive in the same rollout event, adjacent, named almost alike — and a ceiling on the wrong one is
inverted, killing a long cheap focused session and waving through a bloated one. `RALPH_IMPLEMENTER`
unset still means *the argv in `RALPH_AGENT_CMD`*: an agent with no context signal, bounded on the
clock alone, reporting a peak of zero because nobody was watching.

**Copilot backs both roles.** `RALPH_IMPLEMENTER=copilot` and `RALPH_EDITOR=copilot` — so the Editor
need not be the same model as the Implementer, which is the whole point of having two on each side.
Metered on `usage.prompt_tokens` from the `--log-dir` log, which requires `--log-level debug`: at
the default level there are no usage blocks at all, and a session that publishes none is not cheap,
it is **unmetered**, so the adapter raises. Copilot's `--output-format json` stream is a decoy — it
publishes `outputTokens` and never mentions the prompt.

**MCP is disabled on every Copilot run**, by name, from `copilot mcp list --json` — because
`--disable-builtin-mcps` turns off `github-mcp-server` and nothing else, while the cost comes from
plugins and user config. Default launch: **56.5k of context to answer the word "pong"**, 47% of the
smart zone gone before the brief is read. MCP off: 25.9k. Editor's tool allowlist too: 8.4k.

**`ralph validate` refuses; it does not warn.** Five checks — a protected branch, a dirty tree, a
suite it cannot find, a pre-commit hook the repo asks for and never installed, a graph that will not
parse — and each carries **its own sentence**: the graph's refusal quotes the parser verbatim rather
than paraphrasing it, because a cycle and a missing acceptance criterion are different mornings.
The rule is pure (`RepoFacts` in, `Refusal`s out); only the gathering is `cli.py`'s. **`ralph run`
runs the same checks and raises** — a check that fires only when a human remembers to ask for it is
a check the run does not have. `ralph run --dry-run` prints the build order, derived by asking
`eligible` the same question the scheduler asks, never by a second topological sort.

**All eleven sub-issues have landed.** What has *not* happened: no run has yet been driven by a real
model. The suite proves the harness against a scripted stand-in agent — real subprocess, real git,
no intelligence — and that gap is the honest one to close next.

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
- `CODEX_HOME` — where Codex keeps its sessions (default: `~/.codex`). The rollout files the
  context ceiling is read from live under `$CODEX_HOME/sessions/`
- `RALPH_IMPLEMENTER` — `codex` or `copilot` (named only in `cli.py`). **Unset** means
  the argv in `RALPH_AGENT_CMD` — an agent with no context signal, bounded on the clock alone
- `RALPH_REAL_CODEX` — set to `1` to un-skip the one test in the suite that calls a model
- `RALPH_EDITOR` — `claude` or `copilot` (named only in `cli.py`). **Unset** means there
  is no Editor: quarantine-and-drain. `claude` requires the optional `claude-agent-sdk` (`pip
  install -e ".[editor]"`); `copilot` enforces read-only through the CLI's own permission engine,
  which is a **weaker** guarantee than the SDK's — not because the list is shorter, but because it
  is enforced inside a binary the harness cannot inspect or test
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

