# Ralph Loop

Harness engineering for autonomous issue execution via coding agents.

Ralph Loop reads issue files from a target repo's `.scratch/` directory, resolves intra-repo dependencies, spins up isolated git worktrees, and dispatches one coding agent per issue in parallel waves. Each wave blocks until all issues in it are merged, then the next wave begins.

## Prerequisites

- `copilot` CLI for the original GitHub Copilot scripts, or `codex` CLI for the Codex scripts
- Git 2.38+ (worktree support)
- [mattpocock/skills](https://github.com/mattpocock/skills) installed at user level (provides the `/tdd` skill):
  ```bash
  npx skills@latest add mattpocock/skills
  ```

## Usage

```bash
# Validate a target repo before running
./src/validate.sh /path/to/target-repo

# Optionally specify a custom issues directory
./src/validate.sh /path/to/target-repo /path/to/issues-dir

# Run all issues from a .scratch directory in dependency-ordered waves
./src/parallel.sh /path/to/target-repo/.scratch

# Run a single issue (auto-detects git root from the file path)
./src/once.sh /path/to/target-repo/.scratch/01-my-issue.md

# Codex CLI variants
RALPH_AGENT=codex ./src/validate.sh /path/to/target-repo
./src/parallel-codex.sh /path/to/target-repo/.scratch
./src/once-codex.sh /path/to/target-repo/.scratch/01-my-issue.md
```

## Issue format

Issues are Markdown files inside the target repo's `.scratch/` directory:

```markdown
# 01 — Add user authentication

Status: not-started

Brief description of the task.

## Acceptance criteria
- [ ] Users can sign in with email/password
- [ ] Invalid credentials return a 401

## Blocked by
- #00 (database schema)
```

**Status values:** `not-started` → `ready-for-agent` → `in-progress` → `done`

`parallel.sh` sets `Status: done` automatically after a successful merge. Do not set it manually.

**Dependencies:** list blockers in a `## Blocked by` section using `#N` numeric references (matched to `N-*.md` files) or bare filenames. An issue runs only when all its blockers are `done`.

## PRD support

If your issues live inside a subdirectory (e.g. `.scratch/phase-1/issues/`), place a `PRD.md` one level above the issues directory. The once and parallel scripts automatically inject it as design context into each agent invocation.

## Failure recovery

Failed worktrees are preserved at `<repo>/.worktrees/failed/<slug>` for inspection. Active worktrees live at `<repo>/.worktrees/active/`. Merge conflicts also move the worktree to `failed/` rather than corrupting the branch.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `COPILOT_MODEL` | `gpt-5.3-codex` | Model passed to `copilot --model` |
| `CODEX_MODEL` | `gpt-5.3-codex` | Model passed to `codex exec --model` |
| `CODEX_SANDBOX` | `workspace-write` | Sandbox passed to `codex exec --sandbox` |
| `CODEX_APPROVAL` | `never` | Approval policy passed to `codex exec --ask-for-approval` |
| `RALPH_AGENT` | `copilot` | Validation target. Set to `codex` for Codex pre-flight checks |
| `RALPH_CODEX_UNSANDBOXED` | `0` | Set to `1` to pass `--dangerously-bypass-approvals-and-sandbox` to Codex |
| `RALPH_PROTECTED_BRANCHES` | `main master` | Space-separated branches Ralph refuses to run on |

## Design principles

1. **Target repos stay agnostic** — Ralph never modifies target repo structure. It reads `.scratch/` for issues and a repo-level agent context file (`CLAUDE.md` for Copilot, `AGENTS.md` or `CLAUDE.md` for Codex).
2. **Single-repo scope** — Ralph handles intra-repo dependencies only. Cross-repo sequencing is the user's responsibility.
3. **Skills as references** — Ralph's prompt invokes `/tdd` by name. Skills must be installed at user level, not bundled into this repo.
4. **Worktree isolation** — Each issue runs in its own git worktree. Parallel agents are merged sequentially to avoid conflicts.
