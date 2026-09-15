---
name: ralph-init
description: Set up a target repo so a Ralph run can start — discover and prove the repo's test command, then write the `.ralph.toml` command descriptor at its root. Use this whenever someone wants to prepare, initialize, or onboard a repo for Ralph, asks what Ralph should run as its suite, hits the `missing-test-command` refusal from `ralph validate`, or has a `.ralph.toml` that is missing, malformed, or declares a command that does not work in a worktree. Also use it when someone says "init ralph here", "set up .ralph.toml", or "why won't ralph run in this repo".
---

# Initialize a repo for Ralph

Ralph is a harness that runs an issue graph unattended: an Implementer takes one sub-issue per
session in an isolated git worktree, and a merge queue runs **the repo's own test command** on the
prospective merge and fast-forwards only what passes. That command is declared once, in a
`.ralph.toml` at the repo root, and this skill is how it gets there.

**The file is two fields. Getting them right is the whole job.**

```toml
[commands]
test = "uv run pytest -q"   # required — the gate the merge queue runs
install = "uv sync"         # optional — runs once in the base checkout, before any worktree
```

Both strings are split with shell quoting rules. Nothing else in the file is read.

That looks like a thirty-second task and is not, because the command is not run the way you would run
it. Read [the four constraints](#the-four-constraints) before you pick one. **Do not write the file
from a guess** — a descriptor that is syntactically fine and operationally wrong turns the merge
queue into a rubber stamp, and everything a model writes lands.

## The four constraints

Every one of these has produced a plausible-looking descriptor that fails, or worse, passes when it
should not.

### 1. There is no shell

The command is split into argv and executed directly. `&&`, `||`, `;`, `|`, `>`, `*`, `$VAR`, and
`cd` are not operators here — they are literal arguments handed to the program.

```toml
test = "npm ci && npm test"        # execs `npm` with the arguments `ci`, `&&`, `npm`, `test`
test = "cd api && pytest"          # there is no `cd` binary doing what you want
test = "pytest tests/*.py"         # the glob is never expanded
```

If the gate is genuinely several steps, they belong in something that *is* a shell: an npm script, a
Makefile target, a `justfile` recipe, a committed `scripts/test.sh`. The descriptor then invokes that
one entry point. This is not a workaround — a repo whose gate is a named target is a repo where a
human can run the same gate the harness does.

### 2. `test` runs in a fresh worktree; `install` runs in the base checkout

Sessions work in `git worktree add` checkouts, which contain **tracked files only**. Anything
gitignored — `node_modules/`, `.venv/`, `target/`, `dist/`, a generated config — does not exist
there. And `install` deliberately runs *once, in the base checkout*, never per worktree: N worktrees
would mean N installs of the same tree.

So `install` cannot be how the worktree gets its dependencies. Its honest use is warming a **shared,
global** cache — the uv cache, the Go module cache, the Cargo registry — so the first real run is
fast, and failing loudly and early if the repo cannot install at all.

The worktree's dependencies have to come from the `test` command itself:

- **Self-resolving toolchains** need nothing extra. `uv run pytest` syncs the worktree's environment
  from the lockfile before running. `go test ./...` and `cargo test` resolve from global caches.
- **Everything else installs inside its own entry point.** For npm/pnpm/yarn, the descriptor names a
  script (`test = "npm run ci-test"`) whose definition is `npm ci && vitest run` — npm scripts *do*
  get a shell, so the chain is legal there and illegal in the descriptor.

The check: **could this command work in a directory that has never been built in?** If not, it is
the wrong command.

### 3. The test command is also the Editor's allowlist

When a session fails, a read-only Editor is sent into the preserved worktree to diagnose it, and
re-running the suite is the point of that session. So the descriptor's test command is permitted to
it — matched as an **argv prefix**, on top of a small read-only allowlist.

The prefix is exact, and redirection, command substitution and backgrounding are refused separately,
so this is tighter than it sounds: with `test = "bash scripts/test.sh"`, a `bash other.sh` is denied,
because the second element does not match. What the prefix rule *does* leave open is **trailing
arguments** to that exact command.

So the question to ask of a candidate is: *what could this command do with extra arguments appended?*

```toml
test = "make test"    # `make test clean` also matches the prefix — and make will run `clean`
test = "npm test"     # trailing args reach the script; bounded by what the script does with them
test = "uv run pytest -q"   # trailing args are paths and pytest flags. Nothing to hold.
```

Multi-target task runners are the sharp edge, because appending a second target is exactly the thing
they are designed to accept. A specific runner (`uv run pytest -q`, `go test ./...`, `cargo test`) has
nowhere to go. If the gate must be a task-runner target, prefer one whose sibling targets are
harmless, or point the descriptor at a committed executable script (`./scripts/test.sh`) that ignores
arguments it was not given.

### 4. It has to be able to fail

This command is the only thing standing between a model's opinion and the integration branch. A
suite that cannot go red is worse than no suite: every session lands, and the harness reports success
the whole way down.

The failure modes are quiet ones — a runner that exits 0 on zero collected tests, a script whose last
line swallows the exit code, a watch-mode default that never terminates, a test job that only runs on
a tag. You do not reason about this. You [prove it](#step-4--prove-it).

## Step 1 — find the command the repo already trusts

Do not invent a gate. Discover the one this repo has already committed to, in this order:

1. **CI configuration** — `.github/workflows/*.yml`, `.gitlab-ci.yml`, `.circleci/`. This is the best
   source by a wide margin: whatever CI runs on a pull request is already the repo's real definition
   of "green", already runs on a clean checkout, and is already maintained. Take the test step from
   it, minus the CI-only scaffolding (matrix setup, caching, coverage upload).
2. **Task runners** — `Makefile`, `justfile`, `Taskfile.yml`, `package.json` scripts, `pyproject.toml`
   `[tool.*]` sections. A `test` target here is a strong signal, and it satisfies
   [constraint 1](#1-there-is-no-shell) for free.
3. **Contributor docs** — `README.md`, `CONTRIBUTING.md`, `CLAUDE.md`, `AGENTS.md`. Often names the
   command a human is expected to run.
4. **Lockfiles and manifests**, as a last resort, to infer the ecosystem: `uv.lock`, `poetry.lock`,
   `package-lock.json`, `pnpm-lock.yaml`, `go.mod`, `Cargo.toml`.

If the repo already has a `.ralph.toml`, **read it and verify it rather than overwriting it**. It may
be right; if it is wrong, the interesting output is which constraint it violates.

## Step 2 — ask the user what the repo cannot tell you

Some of this is genuinely not discoverable, and guessing it produces a descriptor that looks fine and
behaves badly. Ask — but only about what you could not resolve in Step 1, and ask it all at once
rather than one question per turn.

- **Which command is *the gate*, when several are plausible?** A repo with `test`, `test:unit`,
  `test:e2e`, and `lint` has not told you which of them a merge must pass.
- **Should slow, flaky, or external-dependency tests be in it?** This is a real tradeoff and it is
  the user's to make. The gate runs on every prospective merge, so a 40-minute e2e suite is a 40-minute
  tax per sub-issue — but excluding it means work lands that it would have caught. Say it that way.
- **Does the suite need anything the worktree will not have?** A database, a running service, Docker,
  credentials. The worktree inherits Ralph's environment plus the repo's `.env`, and nothing else runs
  per-worktree to set things up.
- **Is the suite green right now, on a clean checkout?** If it is already red, every session fails,
  nothing ever lands, and the run's only product is a page to a human. Better to learn this now.

## Step 3 — check the candidate against the four constraints

Walk them explicitly, and say what you found. Constraint 2 is the one that bites hardest and is the
easiest to miss from reading alone.

## Step 4 — prove it

Two runs, and neither is optional. The point of this skill is to hand over a descriptor that has been
*observed* to work, not one that was reasoned about.

**Green, from a worktree.** Run the candidate in a throwaway worktree, which is the environment the
merge queue will actually use:

```bash
git worktree add /tmp/ralph-init-check HEAD
cd /tmp/ralph-init-check && <candidate test command>
```

Confirm the exit code is 0 **and that the output shows tests actually ran** — a count, a list, a
summary line. "Exited 0" alone is exactly the failure mode of
[constraint 4](#4-it-has-to-be-able-to-fail).

**Red, on a break.** In the same worktree, break something small on purpose — invert an assertion in
one test — and re-run. A non-zero exit is what proves the gate is a gate. If it still passes, the
command you picked does not run the tests you think it does.

Then clean up: `git worktree remove --force /tmp/ralph-init-check`.

If either run is impossible here — no toolchain installed, a service you cannot start — **say so
plainly and do not claim the descriptor is proven.** Write it, mark clearly which of the two runs was
skipped and why, and tell the user what to run themselves.

## Step 5 — write `.ralph.toml`

At the repo root. Keep it to the two fields; nothing else is read, and extra keys are silently
ignored, which makes them worse than absent. A short comment explaining *why* this command and not
the neighbouring one is worth its line.

Omit `install` entirely unless it earns its place — a global cache to warm, or an install failure
worth discovering before any session opens rather than N times inside them.

## Step 6 — hand off to `ralph validate`

The descriptor answers one of six pre-flight checks. Run the real arbiter and report the rest:

```bash
ralph validate <repo>        # or `uv run ralph validate <repo>` from a Ralph source checkout
```

`missing-test-command` should now be gone. The others are not yours to fix here — report them with
the one-line fix each needs:

| Refusal | What it means | The fix |
|---|---|---|
| `protected-branch` | HEAD is on `main`/`master`, which a run would fast-forward | cut a working branch |
| `uncommitted-changes` | the merge queue would fight the dirty tree | commit or stash |
| `uninstalled-pre-commit-hooks` | the repo configures pre-commit but installed no hook | `pre-commit install` |
| `invalid-issue-source` / `invalid-issue-graph` | the sub-issue graph is missing or malformed | planning, not setup — the `ralph-plan-issues` skill |

If `ralph` is not on PATH here, say that rather than implying the descriptor was validated.

## Worked examples

| Ecosystem | `test` | `install` | Why |
|---|---|---|---|
| Python + uv | `uv run pytest -q` | `uv sync` | `uv run` syncs the worktree from the lockfile itself; `uv sync` up front only warms the shared cache |
| Python + poetry | `poetry run pytest -q` | `poetry install` | poetry's venv is per-directory, so confirm it resolves in a fresh worktree — if not, wrap both steps in a Make target |
| Node | `npm run ci-test` | — | `node_modules/` does not exist in a worktree, so the script itself must be `npm ci && vitest run`; npm scripts get a shell, the descriptor does not |
| Go | `go test ./...` | — | the module cache is global and shared; a worktree needs nothing |
| Rust | `cargo test` | — | registry cache is global; `target/` rebuilds per worktree, which is slow but correct |
| Anything multi-step | `make test` | — | one entry point with a real shell inside it — but check what a second target appended to it would do ([constraint 3](#3-the-test-command-is-also-the-editors-allowlist)) |
