# Ralph Loop — Harness Engineering

## Commands
- Validate: `./src/validate.sh /path/to/repo [issues-dir]`
- Run all: `./src/parallel.sh /path/to/repo/.scratch/<phase>/issues`
- Run one: `./src/once.sh /path/to/repo/.scratch/<phase>/issues/issue-file.md`
- Test the harness: `./tests/run.sh`

## Scripts (`src/`)
- `prompt.md` — Fixed agent prompt injected into every Copilot invocation (requires `/tdd` skill)
- `prompt-codex.md` — Fixed agent prompt injected into every Codex invocation (requires `/tdd` skill)
- `once.sh` — Create a worktree, run one issue, merge back; walks up the filesystem to find the git root automatically
- `parallel.sh` — Wave-based parallel execution; merges each wave sequentially and marks sub-issues `landed` (legacy; not yet migrated to the merge queue)
- `once-codex.sh` — Codex CLI variant of `once.sh`
- `parallel-codex.sh` — Continuous, concurrency-capped scheduler feeding the lock-guarded merge queue: a sub-issue is dispatched once its blockers have landed and lands independently (no waves). See `docs/prd.md` §4.5.
- `lib/mergequeue.sh` — The merge queue: `mq_land_once` holds the merge lock while it rebases onto the integration head, re-runs the suite in the worktree, and fast-forwards. Pure git/bash; covered by `tests/`.
- `validate.sh` — Pre-flight checks: git state, branch protection, issue format, dependency graph, skills, pre-commit hooks

## Issue format
- Issues live in `.scratch/<phase>/issues/` inside the target repo (e.g. `.scratch/refine_data_flow/issues/`)
- `validate.sh` auto-discovers the `issues/` directory under `.scratch/` when no explicit path is given
- Required: `Status:` line — values: `not-started`, `ready`, `in-progress`, `landed`, `needs-human`
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
Place a `PRD.md` one level above the `issues/` dir (i.e. `.scratch/<phase>/PRD.md`). Both `once.sh` and `parallel.sh` inject it as design context.

## Environment variables
- `COPILOT_MODEL` — model passed to `copilot --model` (default: `gpt-5.3-codex`)
- `CODEX_MODEL` — model passed to `codex exec --model` (default: `gpt-5.3-codex`)
- `CODEX_SANDBOX` — sandbox passed to `codex exec --sandbox` (default: `workspace-write`)
- `CODEX_APPROVAL` — approval policy passed to `codex exec --ask-for-approval` (default: `never`)
- `RALPH_AGENT` — validation target, either `copilot` or `codex` (default: `copilot`)
- `RALPH_CODEX_UNSANDBOXED` — set to `1` to pass `--dangerously-bypass-approvals-and-sandbox` to Codex
- `RALPH_PROTECTED_BRANCHES` — space-separated list of branches to refuse running on (default: `main master`)

## Coding Rules
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

