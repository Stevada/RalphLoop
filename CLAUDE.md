# Ralph Loop — Harness Engineering

## Commands
- Validate: `./src/validate.sh /path/to/repo [issues-dir]`
- Run all: `./src/parallel.sh /path/to/repo/.scratch/<phase>/issues`
- Run one: `./src/once.sh /path/to/repo/.scratch/<phase>/issues/issue-file.md`

## Scripts (`src/`)
- `prompt.md` — Fixed agent prompt injected into every Copilot invocation (requires `/tdd` skill)
- `prompt-codex.md` — Fixed agent prompt injected into every Codex invocation (requires `/tdd` skill)
- `once.sh` — Create a worktree, run one issue, merge back; walks up the filesystem to find the git root automatically
- `parallel.sh` — Wave-based parallel execution; merges each wave sequentially and marks issues `done`
- `once-codex.sh` — Codex CLI variant of `once.sh`
- `parallel-codex.sh` — Codex CLI variant of `parallel.sh`
- `validate.sh` — Pre-flight checks: git state, branch protection, issue format, dependency graph, skills, pre-commit hooks

## Issue format
- Issues live in `.scratch/<phase>/issues/` inside the target repo (e.g. `.scratch/refine_data_flow/issues/`)
- `validate.sh` auto-discovers the `issues/` directory under `.scratch/` when no explicit path is given
- Required: `Status:` line — values: `not-started`, `ready-for-agent`, `in-progress`, `done`
- Required for agent context: `## Acceptance criteria` section
- Optional dependencies: `## Blocked by` section with bullet lines — each non-None bullet is matched against sibling issue filenames by numeric prefix (e.g. `#02`, `PDA #02`, `02-remove-payload.md` all match `02-*.md`)
- `parallel.sh` sets `Status: done` automatically after a successful merge; do not set it manually

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
