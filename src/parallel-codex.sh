#!/bin/bash
set -eo pipefail

# parallel-codex.sh - Run issues with Codex CLI in dependency-ordered waves.
# Usage: ./parallel-codex.sh /path/to/repo/.scratch

RALPH_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$1" ]; then
  echo "Usage: $0 <issues-directory>"
  echo ""
  echo "Run issues with Codex CLI in parallel waves, respecting intra-repo dependencies."
  echo "Example: $0 /path/to/repo/.scratch/refine_data_flow/issues"
  exit 1
fi

ISSUES_DIR="$1"

# Resolve to absolute path
if [[ "$ISSUES_DIR" != /* ]]; then
  ISSUES_DIR="$(pwd)/$ISSUES_DIR"
fi

if ! command -v codex &>/dev/null; then
  echo "ERROR: codex CLI not found on PATH"
  exit 1
fi

# Determine target repo from issues directory
TARGET_DIR="$ISSUES_DIR"
while [ "$TARGET_DIR" != "/" ]; do
  if [ -d "$TARGET_DIR/.git" ]; then
    break
  fi
  TARGET_DIR=$(dirname "$TARGET_DIR")
done

if [ ! -d "$TARGET_DIR/.git" ]; then
  echo "ERROR: Could not find git repo root for: $ISSUES_DIR"
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

WORKTREE_BASE="$REPO_ROOT/.worktrees/active"
WORKTREE_FAILED="$REPO_ROOT/.worktrees/failed"
mkdir -p "$WORKTREE_BASE" "$WORKTREE_FAILED"

# Model and Codex execution configuration
MODEL="${CODEX_MODEL:-gpt-5.3-codex}"
CODEX_SANDBOX="${CODEX_SANDBOX:-workspace-write}"
CODEX_APPROVAL="${CODEX_APPROVAL:-never}"

# Read prompt from Ralph repo
PROMPT=$(cat "$RALPH_DIR/prompt-codex.md")

# Find PRD if issues are inside a phase directory
PHASE_DIR=$(dirname "$ISSUES_DIR")
PRD_FILE="$PHASE_DIR/PRD.md"
PRD_CONTENT=""
if [ -f "$PRD_FILE" ]; then
  PRD_CONTENT=$(cat "$PRD_FILE")
fi

# Compute max waves
MAX_WAVES=$(find "$ISSUES_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l)
if [ "$MAX_WAVES" -eq 0 ]; then
  echo "No issue files found in $ISSUES_DIR"
  exit 1
fi

echo "=== Ralph: parallel-codex ==="
echo "Repo:     $REPO_ROOT"
echo "Branch:   $ORIGINAL_BRANCH"
echo "Issues:   $MAX_WAVES file(s) in $ISSUES_DIR"
echo "Model:    $MODEL"
echo "Sandbox:  $CODEX_SANDBOX"
echo "Approval: $CODEX_APPROVAL"
echo ""

declare -A pids branches worktrees

cleanup() {
  for slug in "${!worktrees[@]}"; do
    git worktree remove --force "${worktrees[$slug]}" 2>/dev/null || true
    git branch -d "${branches[$slug]}" 2>/dev/null || true
  done
}
trap cleanup EXIT

get_status() {
  local result
  result=$(grep -m1 "^Status:" "$1" 2>/dev/null | sed 's/Status:[[:space:]]*//' | tr -d '\r') || true
  echo "$result"
}

get_depends_on() {
  local f="$1"
  local issue_dir
  issue_dir=$(dirname "$f")

  # Extract bullet lines under "## Blocked by" (stop at next heading)
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    [[ "$line" == None* ]] && continue
    [[ "$line" == "can start"* ]] && continue

    # Direct filename reference (e.g. "02-remove-payload-tools.md")
    if [[ "$line" =~ \.md$ ]]; then
      local candidate="$issue_dir/$(echo "$line" | sed 's/^[[:space:]]*//')"
      [ -f "$candidate" ] && echo "$candidate"
      continue
    fi

    # Match against sibling issue files: if the sibling's numeric prefix
    # appears in the line as a standalone number, it is a dependency.
    for sibling in "$issue_dir"/*.md; do
      [ "$sibling" = "$f" ] && continue
      [ -f "$sibling" ] || continue
      local prefix
      prefix=$(basename "$sibling" | grep -oP '^\d+')
      [ -z "$prefix" ] && continue
      # Word-boundary match: prefix must not be part of a larger number
      if [[ "$line" =~ (^|[^0-9])${prefix}([^0-9]|$) ]]; then
        echo "$sibling"
      fi
    done
  done < <(awk '/^## Blocked by/{found=1; next} found && /^- /{print substr($0,3)} found && /^#/{exit}' "$f" 2>/dev/null | tr -d '\r')
}

is_ready() {
  local f="$1"
  local status
  status=$(get_status "$f")
  [ "$status" = "not-started" ] || [ "$status" = "ready-for-agent" ] || return 1
  while IFS= read -r dep; do
    [ -z "$dep" ] && continue
    [ -f "$dep" ] || continue
    [ "$(get_status "$dep")" = "done" ] || return 1
  done < <(get_depends_on "$f")
}

run_codex() {
  local worktree="$1"
  local full_prompt="$2"

  if [ "${RALPH_CODEX_UNSANDBOXED:-0}" = "1" ]; then
    codex exec \
      -C "$worktree" \
      --model "$MODEL" \
      --dangerously-bypass-approvals-and-sandbox \
      "$full_prompt" > "$worktree/ralph-output.txt" 2>&1
  else
    codex exec \
      -C "$worktree" \
      --model "$MODEL" \
      --sandbox "$CODEX_SANDBOX" \
      --ask-for-approval "$CODEX_APPROVAL" \
      "$full_prompt" > "$worktree/ralph-output.txt" 2>&1
  fi
}

stall_count=0
TOTAL_START=$SECONDS

for ((wave=1; wave<=MAX_WAVES; wave++)); do
  echo ""
  echo "=== Wave $wave / $MAX_WAVES ==="

  # Find ready issues
  mapfile -t ready_issues < <(
    find "$ISSUES_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | while read -r f; do
      is_ready "$f" && echo "$f"
    done | sort
  )

  if [ ${#ready_issues[@]} -eq 0 ]; then
    # Check if all done
    all_done=true
    while IFS= read -r f; do
      status=$(get_status "$f")
      if [ "$status" != "done" ]; then
        all_done=false
        break
      fi
    done < <(find "$ISSUES_DIR" -maxdepth 1 -name "*.md" 2>/dev/null)

    if [ "$all_done" = true ]; then
      echo "All issues done."
      echo ""
      echo "=== Timing Summary ==="
      [ -f "$WORKTREE_BASE/timings.txt" ] && cat "$WORKTREE_BASE/timings.txt"
      echo "Total elapsed: $(( SECONDS - TOTAL_START ))s"
      exit 0
    fi

    stall_count=$((stall_count + 1))
    if [ $stall_count -ge 2 ]; then
      echo "STALLED: No ready issues for 2 consecutive waves."
      echo "Remaining:"
      find "$ISSUES_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | while read -r f; do
        status=$(get_status "$f")
        [ "$status" != "done" ] && echo "  $(basename "$f") (Status: $status)"
      done
      exit 1
    fi
    echo "No ready issues this wave. Waiting for dependencies..."
    continue
  fi

  stall_count=0
  echo "Launching ${#ready_issues[@]} worker(s):"
  for f in "${ready_issues[@]}"; do echo "  $(basename "$f")"; done

  # Reset tracking
  unset pids branches worktrees issue_files
  declare -A pids branches worktrees issue_files

  # Launch workers
  for issue_file in "${ready_issues[@]}"; do
    slug=$(basename "$issue_file" .md)
    branch="ralph/$slug"
    worktree="$WORKTREE_BASE/$slug"

    # Clean stale worktree
    if [ -d "$worktree" ]; then
      git worktree remove --force "$worktree" 2>/dev/null || true
      git branch -D "$branch" 2>/dev/null || true
    fi

    git worktree add "$worktree" -b "$branch" >/dev/null

    (
      cd "$worktree"
      # Install deps
      if [ -f "pyproject.toml" ] && command -v poetry &>/dev/null; then
        poetry install --no-root -q 2>/dev/null || true
      elif [ -f "package.json" ] && command -v npm &>/dev/null; then
        npm ci --quiet 2>/dev/null || true
      fi

      start_time=$SECONDS
      issue_content=$(cat "$issue_file")
      commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")

      scope_note="You are working in an isolated git worktree of: $REPO_ROOT
Work ONLY on the assigned issue below. Other agents are running in parallel, so keep changes scoped.

PRD (design context):
$PRD_CONTENT"

      full_prompt="$scope_note

Assigned issue ($issue_file):
$issue_content

Previous commits: $commits
$PROMPT"

      run_codex "$worktree" "$full_prompt"

      elapsed=$(( SECONDS - start_time ))
      echo "$slug: ${elapsed}s" >> "$WORKTREE_BASE/timings.txt"
    ) &

    pids["$slug"]=$!
    branches["$slug"]="$branch"
    worktrees["$slug"]="$worktree"
    issue_files["$slug"]="$issue_file"
  done

  # Wait for workers
  echo ""
  echo "Waiting for workers..."
  declare -A failed_slugs
  for slug in "${!pids[@]}"; do
    if wait "${pids[$slug]}"; then
      echo "  $slug: finished"
    else
      echo "  $slug: FAILED (preserved at ${worktrees[$slug]})"
      failed_slugs["$slug"]=1
    fi
  done

  # Merge successful, mark done
  for slug in "${!pids[@]}"; do
    if [ "${failed_slugs[$slug]:-0}" = "1" ]; then
      # Move failed worktree
      failed_path="$WORKTREE_FAILED/$slug"
      rm -rf "$failed_path"
      mv "${worktrees[$slug]}" "$failed_path"
      git worktree prune 2>/dev/null || true
      git branch -D "${branches[$slug]}" 2>/dev/null || true
      continue
    fi

    branch="${branches[$slug]}"
    wt="${worktrees[$slug]}"
    issue_file="${issue_files[$slug]}"

    # Check for commits
    NEW_COMMITS=$(git log "$ORIGINAL_BRANCH..$branch" --oneline 2>/dev/null | wc -l)
    if [ "$NEW_COMMITS" -eq 0 ]; then
      echo "  $slug: no commits - skipping"
      git worktree remove --force "$wt" 2>/dev/null || true
      git branch -d "$branch" 2>/dev/null || true
      continue
    fi

    # Merge
    if git merge "$branch" --no-edit 2>/dev/null; then
      echo "  $slug: merged $NEW_COMMITS commit(s)"
      # Mark issue as done
      sed -i "s/^Status:.*/Status: done/" "$issue_file"
      git add "$issue_file" 2>/dev/null || true
      git commit -m "ralph: mark $slug as done" --no-verify 2>/dev/null || true
    else
      echo "  $slug: MERGE CONFLICT - aborting merge, preserving worktree"
      git merge --abort 2>/dev/null || true
      failed_path="$WORKTREE_FAILED/$slug"
      rm -rf "$failed_path"
      mv "$wt" "$failed_path"
      git worktree prune 2>/dev/null || true
      git branch -D "$branch" 2>/dev/null || true
      continue
    fi

    git worktree remove --force "$wt" 2>/dev/null || true
    git branch -d "$branch" 2>/dev/null || true
  done
done

echo ""
echo "=== Complete ==="
echo "Total elapsed: $(( SECONDS - TOTAL_START ))s"
