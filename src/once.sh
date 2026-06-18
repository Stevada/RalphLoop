#!/bin/bash
set -eo pipefail

# once.sh — Run a single issue in a worktree, then merge back.
# Usage: ./once.sh /path/to/repo/.scratch/03-my-issue.md

RALPH_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$1" ]; then
  echo "Usage: $0 <issue-file>"
  echo ""
  echo "Run a single issue in a worktree, then merge back."
  echo "Example: $0 /path/to/repo/.scratch/refine_data_flow/issues/03-my-issue.md"
  exit 1
fi

ISSUE_FILE="$1"

# Resolve to absolute path
if [[ "$ISSUE_FILE" != /* ]]; then
  ISSUE_FILE="$(pwd)/$ISSUE_FILE"
fi

if [ ! -f "$ISSUE_FILE" ]; then
  echo "Issue file not found: $ISSUE_FILE"
  exit 1
fi

# Determine target repo from issue file location
# Walk up from the issue file to find .git
TARGET_DIR=$(dirname "$ISSUE_FILE")
while [ "$TARGET_DIR" != "/" ]; do
  if [ -d "$TARGET_DIR/.git" ]; then
    break
  fi
  TARGET_DIR=$(dirname "$TARGET_DIR")
done

if [ ! -d "$TARGET_DIR/.git" ]; then
  echo "ERROR: Could not find git repo root for issue file: $ISSUE_FILE"
  exit 1
fi

REPO_ROOT="$TARGET_DIR"

cd "$REPO_ROOT"
ORIGINAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$ORIGINAL_BRANCH" = "HEAD" ]; then
  echo "ERROR: HEAD is detached. Check out a branch before running Ralph."
  exit 1
fi

# Protect trunk branches from direct merges
PROTECTED_BRANCHES="${RALPH_PROTECTED_BRANCHES:-main master}"
for protected in $PROTECTED_BRANCHES; do
  if [ "$ORIGINAL_BRANCH" = "$protected" ]; then
    echo "ERROR: Refusing to run on protected branch '$ORIGINAL_BRANCH'."
    echo "Create a feature branch first: git checkout -b feature/my-work"
    exit 1
  fi
done

# Model configuration
MODEL="${COPILOT_MODEL:-gpt-5.3-codex}"

# Derive slug, branch, worktree
slug=$(basename "$ISSUE_FILE" .md)
branch="ralph/$slug"
mkdir -p "$REPO_ROOT/.worktrees/active" "$REPO_ROOT/.worktrees/failed"
worktree="$REPO_ROOT/.worktrees/active/$slug"

# Find PRD if issue is inside an issues/ subdirectory
PHASE_DIR=$(dirname "$(dirname "$ISSUE_FILE")")
PRD_FILE="$PHASE_DIR/PRD.md"
PRD_CONTENT=""
if [ -f "$PRD_FILE" ]; then
  PRD_CONTENT=$(cat "$PRD_FILE")
fi

# Read prompt from Ralph repo
PROMPT=$(cat "$RALPH_DIR/prompt.md")

echo "=== Ralph: once ==="
echo "Repo:   $REPO_ROOT"
echo "Branch: $ORIGINAL_BRANCH"
echo "Issue:  $(basename "$ISSUE_FILE")"
echo "Model:  $MODEL"
echo ""

# Create worktree
if [ -d "$worktree" ]; then
  echo "Removing stale worktree: $worktree"
  git worktree remove --force "$worktree" 2>/dev/null || true
  git branch -D "$branch" 2>/dev/null || true
fi

git worktree add "$worktree" -b "$branch" >/dev/null

cleanup() {
  if [ -d "$worktree" ]; then
    git worktree remove --force "$worktree" 2>/dev/null || true
    git branch -d "$branch" 2>/dev/null || true
  fi
}

cd "$worktree"

# Install dependencies if applicable
if [ -f "pyproject.toml" ] && command -v poetry &>/dev/null; then
  poetry install --no-root -q 2>/dev/null || true
elif [ -f "package.json" ] && command -v npm &>/dev/null; then
  npm ci --quiet 2>/dev/null || true
fi

start_time=$SECONDS
issue_content=$(cat "$ISSUE_FILE")
commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")

scope_note="You are working in an isolated git worktree of: $REPO_ROOT
Work ONLY on the assigned issue below.

PRD (design context):
$PRD_CONTENT"

echo "Launching Copilot agent..."
if copilot -p "$scope_note

Assigned issue ($ISSUE_FILE):
$issue_content

Previous commits: $commits
$PROMPT" \
  --model="$MODEL" \
  --allow-all \
  --no-ask-user \
  -s > "$worktree/ralph-output.txt" 2>&1; then

  elapsed=$(( SECONDS - start_time ))
  echo "Agent completed in ${elapsed}s"

  # Check if agent produced commits
  NEW_COMMITS=$(cd "$worktree" && git log "$ORIGINAL_BRANCH..$branch" --oneline 2>/dev/null | wc -l)
  if [ "$NEW_COMMITS" -eq 0 ]; then
    echo "No commits produced. Issue may already be done or not actionable."
    cleanup
    exit 0
  fi

  echo "$NEW_COMMITS commit(s) produced. Merging into $ORIGINAL_BRANCH..."
  cd "$REPO_ROOT"
  git merge "$branch" --no-edit
  git worktree remove --force "$worktree" 2>/dev/null || true
  git branch -d "$branch" 2>/dev/null || true

  echo "Done. Merged $NEW_COMMITS commit(s)."
else
  elapsed=$(( SECONDS - start_time ))
  echo "Agent FAILED after ${elapsed}s. Worktree preserved at: $worktree"
  # Move to failed directory
  failed_path="$REPO_ROOT/.worktrees/failed/$slug"
  rm -rf "$failed_path"
  mv "$worktree" "$failed_path"
  git worktree prune 2>/dev/null || true
  git branch -D "$branch" 2>/dev/null || true
  echo "Failed worktree moved to: $failed_path"
  exit 1
fi
