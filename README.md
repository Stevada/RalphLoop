# Ralph

Harness engineering for autonomous issue execution via coding agents.

Ralph reads issues from a target repo's `.scratch/` directory, resolves intra-repo dependencies, spins up git worktrees, and dispatches one Copilot agent per issue in parallel waves.

## Prerequisites

- `copilot` CLI (GitHub Copilot CLI agent)
- Git 2.38+ (worktree support)
- [mattpocock/skills](https://github.com/mattpocock/skills) installed at user level:
  ```bash
  npx skills@latest add mattpocock/skills
  ```

## Usage

```bash
# Validate a target repo before running
./validate.sh /path/to/target-repo

# Run all issues from a target repo's .scratch directory
./parallel.sh /path/to/target-repo/.scratch

# Run a single issue
./once.sh /path/to/target-repo/.scratch/01-my-issue.md
```

## Design Principles

1. **Target repos stay agnostic** — Ralph never modifies repo structure. It reads `.scratch/` for issues and `CLAUDE.md` for project context.
2. **Single-repo scope** — Ralph handles intra-repo dependencies only. Cross-repo sequencing is the user's job.
3. **Skills as references** — Ralph's prompt invokes `/tdd` by name. Skills must be installed at user level, not bundled.
4. **Worktree isolation** — Each issue runs in its own git worktree. Parallel agents can't conflict.
