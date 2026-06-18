# Ralph — Harness Engineering

## Commands
- Validate: `./validate.sh /path/to/repo`
- Run all: `./parallel.sh /path/to/repo/.scratch`
- Run one: `./once.sh /path/to/repo/.scratch/issue-file.md`

## Structure
- `prompt.md` — Fixed agent prompt (references /tdd skill)
- `once.sh` — Execute a single issue in a worktree
- `parallel.sh` — Wave-based parallel execution with dependency resolution
- `validate.sh` — Pre-flight checks before launching

## Conventions
- Issues live in target repo's `.scratch/` directory
- Issues must have `Status:` line (not-started, ready-for-agent, in-progress, done)
- Dependencies declared via `## Blocked by` section with issue references
- Target repos must have `CLAUDE.md` at root
