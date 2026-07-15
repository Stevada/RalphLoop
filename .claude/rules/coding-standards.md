# Coding rules

*How to work, and what to build,* in this repo. Loaded every session — no `paths` scope — because
the behavioural half applies to all work, docs included. The design rationale behind these
conventions is in [`docs/design.md`](../../docs/design.md).

**One Ralph adaptation, and it matters:** *"if something is unclear, ask"* below assumes a human in
the session. An unattended Implementer has none — its way of asking is **`<impasse>`**, with a
structured report. Guessing, or softening an acceptance criterion until it passes, is the failure
mode the whole harness exists to catch.

---

## How to work

Guidelines to reduce common LLM coding mistakes. They bias toward caution over speed; for trivial
tasks, use judgment.

### Think before coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### Simplicity first

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### Surgical changes

**Touch only what you must. Clean up only your own mess.**

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.
- Remove imports/variables/functions that YOUR changes made unused; leave pre-existing dead code.

The test: every changed line should trace directly to the user's request.

### Goal-driven execution

**Define success criteria. Loop until verified.**

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant
clarification.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to
overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## What to build

### 1. Type discipline

- Avoid `Any` — define explicit types, Protocols, or TypedDicts for all function signatures, return
  values, and module-level variables.
- When wrapping third-party SDKs that return untyped objects, define a local Protocol or TypedDict
  describing the shape we actually use.
- `Any` is acceptable only at import-fallback boundaries (`except ImportError` guards) — mark those
  with `# type: ignore` and a comment explaining why.

### 2. DRY — Don't Repeat Yourself

- Every piece of knowledge must have a single, unambiguous, authoritative representation.
- If you find yourself copying logic, extract it into a shared function, class, or module.
- Configuration values, magic strings, and constants must be defined once and imported — never
  duplicated across files.

### 3. Single source of truth

- Each concept (schema, config shape, harness type, business rule) is owned by exactly one module.
- Other modules import from the owner — they never redefine or shadow it.
- When a third-party type needs adaptation, create one wrapper/protocol in one place; consumers
  depend on that wrapper.
- If two modules need the same data shape, move it to a shared location rather than defining it twice.

### 4. Architecture conventions

[`docs/architecture.md`](../../docs/architecture.md) is the map. Layers, and the dependency arrow
points inward: `harness/` → `ports.py` → `adapters/` → orchestration → `cli.py`.

- **`harness/` is pure.** Stdlib imports only. No I/O, no subprocess, no git, no model. If a harness
  function needs a fact from the world, it takes it as an argument.
- **`ralph/harness/__init__.py` is the harness interface.** Import `from ralph.harness import Outcome`,
  never `from ralph.harness.model.session import Outcome`. The layout inside is an implementation
  detail; callers should not have to learn it.
- **`harness/model/` is the nouns; `harness/rules/` is the verbs.** `model/` holds frozen values with
  zero logic. `rules/` holds the pure functions that *are* the design — the failure taxonomy, the
  routing table, eligibility, the cycle cap. **`rules/` may import `model/`; `model/` may not import
  `rules/`** — a test enforces it. A value that knows how it will be classified has stopped being a
  value. New decision logic goes in `rules/`, never beside the type it decides about.
- **The harness core is not split by actor, and `adapters/` is not split by port.** `Outcome` and
  `SessionTelemetry` belong to both actors; `copilot.py` is both an Implementer and an Editor.
  Grouping either way forces a `shared/` folder that swallows everything.
- **The fakes live in `tests/fakes.py`, never in `ralph/`.** Nothing in the shipped package may
  import from `tests/` — a test asserts it. An adapter that reaches for a fake has stopped being an
  adapter. `tests/builders.py` is a separate thing: builders make *values*, fakes satisfy *Protocols*.
- **Structure is a frozen value; content is state.** `@dataclass(frozen=True, slots=True)` for harness
  types. `IssueGraph` and `SubIssue` are immutable for a run's whole life — the invariant "the Editor
  may never re-link a sub-issue" is enforced by the type, not by a rule someone must remember.
- **Enums, not strings.** `Outcome`, `Verdict`, `SubIssueState`, `Destination` are `StrEnum`. A raw
  outcome string anywhere outside a parser is a defect.
- **Every Protocol in `ports.py` has a fake**, and the fakes are what the test suite runs against. A
  test that needs a real model, a real network, or a real `codex` binary is in the wrong layer.
- **One process, asyncio.** The merge lock is an `asyncio.Lock`. No `flock`, no PID files, no polling
  for result files.
- **Concrete adapters are named in `cli.py` and nowhere else.** Nothing downstream knows whether the
  Implementer is Codex or Copilot, or the Editor is Claude Code or Copilot.

### 5. Failure discipline

The harness's whole value is that it classifies failure honestly. Code that blurs a failure is worse
than code that has one.

- **Fail fast, loudly.** Raise on missing required fields — no silent defaults. Never `|| true`,
  never a bare `except:`, never swallow a subprocess's exit code.
- **Zero commits is never a benign skip.** It is an `impasse` or `infra-failed`.
- **The suite result, not the exit code, is the outcome.** The harness runs the tests. A model's exit
  code is its opinion; the suite is a fact.
- **No string-keyed intermediates.** Typed records throughout; no `dict[str, Any]` layers between a
  CLI's JSON and a harness type — parse at the boundary, into a dataclass.
