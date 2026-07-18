# Coding rules

*How to work, and what to build.* Loaded every session — no `paths` scope — because the behavioural
half applies to all work, docs included.

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

- Each concept (a schema, a config shape, a type, a business rule) is owned by exactly one module.
- Other modules import from the owner — they never redefine or shadow it.
- When a third-party type needs adaptation, create one wrapper/protocol in one place; consumers
  depend on that wrapper.
- If two modules need the same data shape, move it to a shared location rather than defining it twice.

### 4. KISS — Keep It Simple, Stupid

- The simplest design that meets the acceptance criteria wins. Clever beats simple only when simple
  cannot do the job — and it rarely can't.
- Prefer a plain function to a class, a class to a hierarchy, a stdlib call to a dependency.
- Reach for the smallest construct that works. If a reader needs a diagram to follow control flow,
  it's too clever.
- This is the *how-to-build* twin of **How to work → Simplicity first** — that section is the habit,
  this is the standard it enforces.

### 5. YAGNI — You Aren't Gonna Need It

- Build for the requirement in front of you, not the one you imagine. No hooks, flags, or seams for a
  future that hasn't been asked for.
- Delete speculative generality on sight: an interface with one implementer, a parameter no caller
  passes, a branch no input reaches.
- The moment a second use actually arrives, generalize then — with the second case in hand, not
  guessed at. See **How to work → Surgical changes**.

### 6. SOLID

Object-level design, for classes and their collaborators.

- **Single responsibility.** Each module, class, and function has one reason to change. A function
  that both makes a decision *and* performs I/O has two.
- **Open/closed.** Extend behaviour by adding a new implementation or case, not by editing a stable
  core. A new variant arrives as a new type, never as another branch in a growing `if`/`switch` on a
  kind field.
- **Liskov substitution.** Every implementation is fully substitutable for the interface it
  satisfies — a caller holding the abstraction must never need to know which concrete type it has.
- **Interface segregation.** Keep interfaces narrow. A caller depends only on the methods it uses;
  split an interface before you widen it past one job.
- **Dependency inversion.** High-level policy depends on abstractions, not on concrete
  implementations; the concrete types are wired together at a single composition root. The dependency
  arrow points toward the abstraction.

### 7. Failure discipline

Code that blurs a failure is worse than code that has one.

- **Fail fast, loudly.** Raise on missing required fields — no silent defaults. Never `|| true`,
  never a bare `except:`, never swallow a subprocess's exit code.
- **No string-keyed intermediates.** Typed records throughout; no `dict[str, Any]` layers — parse at
  the boundary, into a dataclass.
