# Ralph Loop — Harness Engineering

## Commands
- Validate: `./src/validate.sh /path/to/repo [issues-dir]`
- Run all: `./src/parallel.sh /path/to/repo/.scratch/<phase>/issues`
- Run one: `./src/once.sh /path/to/repo/.scratch/<phase>/issues/issue-file.md`

## Scripts (`src/`)
- `prompt.md` — Fixed agent prompt injected into every Copilot invocation (requires `/tdd` skill)
- `once.sh` — Create a worktree, run one issue, merge back; walks up the filesystem to find the git root automatically
- `parallel.sh` — Wave-based parallel execution; merges each wave sequentially and marks issues `done`
- `validate.sh` — Pre-flight checks: git state, branch protection, issue format, dependency graph, skills, pre-commit hooks

## Issue format
- Issues live in `.scratch/<phase>/issues/` inside the target repo (e.g. `.scratch/refine_data_flow/issues/`)
- `validate.sh` auto-discovers the `issues/` directory under `.scratch/` when no explicit path is given
- Required: `Status:` line — values: `not-started`, `ready-for-agent`, `in-progress`, `done`
- Required for agent context: `## Acceptance criteria` section
- Optional dependencies: `## Blocked by` section with bullet lines — each non-None bullet is matched against sibling issue filenames by numeric prefix (e.g. `#02`, `PDA #02`, `02-remove-payload.md` all match `02-*.md`)
- `parallel.sh` sets `Status: done` automatically after a successful merge; do not set it manually

## Target repo requirements
- Must have `CLAUDE.md` at root (agent reads it for project context)
- Must be on a non-protected branch before running (default protected: `main`, `master`)
- Worktrees are created at `<repo>/.worktrees/active/` and failures preserved at `<repo>/.worktrees/failed/`

## PRD support
Place a `PRD.md` one level above the `issues/` dir (i.e. `.scratch/<phase>/PRD.md`). Both `once.sh` and `parallel.sh` inject it as design context.

## Environment variables
- `COPILOT_MODEL` — model passed to `copilot --model` (default: `gpt-5.3-codex`)
- `RALPH_PROTECTED_BRANCHES` — space-separated list of branches to refuse running on (default: `main master`)
