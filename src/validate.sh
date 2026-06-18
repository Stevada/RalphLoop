#!/bin/bash
set -eo pipefail

# validate.sh — Pre-flight checks for a target repo before running Ralph.
# Usage: ./validate.sh /path/to/target-repo [issues-directory]

if [ -z "$1" ]; then
  echo "Usage: $0 <target-repo-path> [issues-directory]"
  echo ""
  echo "Validates that a target repo is ready for Ralph to execute issues."
  echo "If issues-directory is omitted, auto-discovers under <target-repo>/.scratch:"
  echo "  - Flat:   .scratch/*.md"
  echo "  - Nested: .scratch/<phase>/issues/*.md  (e.g. .scratch/refine_data_flow/issues)"
  exit 1
fi

TARGET_REPO="$1"
ISSUES_DIR="${2:-}"

# Auto-discover issues directory if not explicitly provided
if [ -z "$ISSUES_DIR" ]; then
  SCRATCH_DIR="$TARGET_REPO/.scratch"
  if [ -d "$SCRATCH_DIR" ]; then
    flat_count=$(find "$SCRATCH_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l)
    if [ "$flat_count" -gt 0 ]; then
      ISSUES_DIR="$SCRATCH_DIR"
    else
      # Look for nested .scratch/<phase>/issues/ directories
      mapfile -t nested_dirs < <(find "$SCRATCH_DIR" -mindepth 2 -maxdepth 2 -type d -name "issues" 2>/dev/null | sort)
      if [ "${#nested_dirs[@]}" -eq 1 ]; then
        ISSUES_DIR="${nested_dirs[0]}"
        echo "(Auto-detected issues directory: $ISSUES_DIR)"
      elif [ "${#nested_dirs[@]}" -gt 1 ]; then
        echo "Multiple issue directories found under $SCRATCH_DIR. Specify one explicitly:"
        printf '  %s\n' "${nested_dirs[@]}"
        echo ""
        echo "Usage: $0 $TARGET_REPO <issues-directory>"
        exit 1
      else
        ISSUES_DIR="$SCRATCH_DIR"
      fi
    fi
  else
    ISSUES_DIR="$SCRATCH_DIR"
  fi
fi

ERRORS=0
WARNINGS=0

error() { echo "  ERROR: $1"; ERRORS=$((ERRORS + 1)); }
warn()  { echo "  WARN:  $1"; WARNINGS=$((WARNINGS + 1)); }
ok()    { echo "  OK:    $1"; }

echo "=== Ralph Pre-flight Validation ==="
echo "Target: $TARGET_REPO"
echo "Issues: $ISSUES_DIR"
echo ""

# ─── 1. Target repo basics ───────────────────────────────────────────────────

echo "1. Repository checks"

if [ ! -d "$TARGET_REPO/.git" ]; then
  error "Not a git repository: $TARGET_REPO"
else
  ok "Git repository found"
fi

if [ ! -f "$TARGET_REPO/CLAUDE.md" ]; then
  error "Missing CLAUDE.md at repo root (required for agent context)"
else
  ok "CLAUDE.md found"
fi

# Check not on a protected branch
CURRENT_BRANCH=$(cd "$TARGET_REPO" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "DETACHED")
PROTECTED_BRANCHES="${RALPH_PROTECTED_BRANCHES:-main master}"
for protected in $PROTECTED_BRANCHES; do
  if [ "$CURRENT_BRANCH" = "$protected" ]; then
    error "Target repo is on protected branch '$CURRENT_BRANCH'. Create a feature branch first."
  fi
done
if [ "$CURRENT_BRANCH" = "DETACHED" ]; then
  error "Target repo has detached HEAD. Check out a branch."
else
  ok "On branch: $CURRENT_BRANCH"
fi

# Check for uncommitted changes
if (cd "$TARGET_REPO" && ! git diff --quiet 2>/dev/null) || (cd "$TARGET_REPO" && ! git diff --cached --quiet 2>/dev/null); then
  warn "Target repo has uncommitted changes. Worktrees will branch from current HEAD."
fi

echo ""

# ─── 2. Issues directory ─────────────────────────────────────────────────────

echo "2. Issues directory"

if [ ! -d "$ISSUES_DIR" ]; then
  error "Issues directory not found: $ISSUES_DIR"
else
  ISSUE_COUNT=$(find "$ISSUES_DIR" -maxdepth 1 -name "*.md" 2>/dev/null | wc -l)
  if [ "$ISSUE_COUNT" -eq 0 ]; then
    error "No .md issue files found in $ISSUES_DIR"
  else
    ok "$ISSUE_COUNT issue file(s) found"
  fi
fi

echo ""

# ─── 3. Issue file validation ────────────────────────────────────────────────

echo "3. Issue file validation"

if [ -d "$ISSUES_DIR" ]; then
  for issue_file in "$ISSUES_DIR"/*.md; do
    [ -f "$issue_file" ] || continue
    slug=$(basename "$issue_file")

    # Check Status line exists
    if ! grep -q "^Status:" "$issue_file"; then
      error "$slug: Missing 'Status:' line"
    fi

    # Check for acceptance criteria
    if ! grep -q "## Acceptance criteria" "$issue_file"; then
      warn "$slug: Missing '## Acceptance criteria' section"
    fi
  done

  # Validate dependencies resolve
  for issue_file in "$ISSUES_DIR"/*.md; do
    [ -f "$issue_file" ] || continue
    slug=$(basename "$issue_file")

    # Each non-None bullet under "## Blocked by" is a dependency.
    # Match against sibling issue files by numeric prefix.
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      [[ "$line" == None* ]] && continue
      [[ "$line" == "can start"* ]] && continue

      # Direct filename reference
      if [[ "$line" =~ \.md$ ]]; then
        candidate=$(echo "$line" | sed 's/^[[:space:]]*//')
        if [ ! -f "$ISSUES_DIR/$candidate" ]; then
          warn "$slug: Blocked-by filename '$candidate' does not exist"
        fi
        continue
      fi

      # Try to match against sibling numeric prefixes
      matched=false
      for sibling in "$ISSUES_DIR"/*.md; do
        [ "$sibling" = "$issue_file" ] && continue
        [ -f "$sibling" ] || continue
        prefix=$(basename "$sibling" | grep -oP '^\d+')
        [ -z "$prefix" ] && continue
        if [[ "$line" =~ (^|[^0-9])${prefix}([^0-9]|$) ]]; then
          matched=true
          break
        fi
      done
      if [ "$matched" = false ]; then
        warn "$slug: Blocked-by line '$line' does not match any sibling issue"
      fi
    done < <(awk '/^## Blocked by/{found=1; next} found && /^- /{print substr($0,3)} found && /^#/{exit}' "$issue_file" 2>/dev/null | tr -d '\r')
  done

  ok "Dependency references checked"
fi

echo ""

# ─── 4. Circular dependency detection ────────────────────────────────────────

echo "4. Circular dependency detection"

if [ -d "$ISSUES_DIR" ]; then
  # Build adjacency list and run simple cycle detection
  declare -A DEPS
  for issue_file in "$ISSUES_DIR"/*.md; do
    [ -f "$issue_file" ] || continue
    slug=$(basename "$issue_file" .md)
    dep_slugs=""

    while IFS= read -r line; do
      [ -z "$line" ] && continue
      [[ "$line" == None* ]] && continue
      [[ "$line" == "can start"* ]] && continue

      # Match against sibling numeric prefixes
      for sibling in "$ISSUES_DIR"/*.md; do
        [ "$sibling" = "$issue_file" ] && continue
        [ -f "$sibling" ] || continue
        prefix=$(basename "$sibling" | grep -oP '^\d+')
        [ -z "$prefix" ] && continue
        if [[ "$line" =~ (^|[^0-9])${prefix}([^0-9]|$) ]]; then
          dep_slugs="$dep_slugs $(basename "$sibling" .md)"
        fi
      done
    done < <(awk '/^## Blocked by/{found=1; next} found && /^- /{print substr($0,3)} found && /^#/{exit}' "$issue_file" 2>/dev/null | tr -d '\r')

    DEPS["$slug"]="$dep_slugs"
  done

  # Simple DFS cycle detection
  declare -A VISITED STACK
  has_cycle=false

  dfs() {
    local node="$1"
    VISITED["$node"]=1
    STACK["$node"]=1
    for dep in ${DEPS[$node]}; do
      if [ "${STACK[$dep]:-0}" = "1" ]; then
        error "Circular dependency: $node -> $dep"
        has_cycle=true
        return
      fi
      if [ "${VISITED[$dep]:-0}" != "1" ]; then
        dfs "$dep"
      fi
    done
    STACK["$node"]=0
  }

  for slug in "${!DEPS[@]}"; do
    if [ "${VISITED[$slug]:-0}" != "1" ]; then
      dfs "$slug"
    fi
  done

  if [ "$has_cycle" = false ]; then
    ok "No circular dependencies detected"
  fi
fi

echo ""

# ─── 5. Worktree directory ───────────────────────────────────────────────────

echo "5. Worktree readiness"

WORKTREE_BASE="$TARGET_REPO/.worktrees/active"
if [ -d "$WORKTREE_BASE" ]; then
  ACTIVE_COUNT=$(ls "$WORKTREE_BASE" 2>/dev/null | wc -l)
  if [ "$ACTIVE_COUNT" -gt 0 ]; then
    warn "$ACTIVE_COUNT active worktree(s) exist in $WORKTREE_BASE — may conflict"
  else
    ok "Worktree directory clean"
  fi
else
  ok "Worktree directory will be created on first run"
fi

echo ""

# ─── 6. Skills check ─────────────────────────────────────────────────────────

echo "6. Required skills"

REQUIRED_SKILLS="tdd"
for skill in $REQUIRED_SKILLS; do
  if [ -f "$HOME/.agents/skills/$skill/SKILL.md" ]; then
    ok "Skill '$skill' installed"
  else
    warn "Skill '$skill' not found at ~/.agents/skills/$skill/SKILL.md"
    warn "  Install via: npx skills@latest add mattpocock/skills"
  fi
done

echo ""

# ─── 7. Pre-commit hooks ─────────────────────────────────────────────────────

echo "7. Pre-commit hooks"

if [ -f "$TARGET_REPO/.pre-commit-config.yaml" ] || [ -d "$TARGET_REPO/.git/hooks" ]; then
  if [ -f "$TARGET_REPO/.pre-commit-config.yaml" ]; then
    ok "pre-commit config found"
  fi
  if [ -x "$TARGET_REPO/.git/hooks/pre-commit" ]; then
    ok "pre-commit hook is executable"
  else
    warn "No executable pre-commit hook found — agent commits won't be validated"
  fi
else
  warn "No pre-commit configuration found in target repo"
fi

echo ""

# ─── Summary ─────────────────────────────────────────────────────────────────

echo "=== Summary ==="
echo "Errors:   $ERRORS"
echo "Warnings: $WARNINGS"

if [ "$ERRORS" -gt 0 ]; then
  echo ""
  echo "FAILED: Fix errors before running Ralph."
  exit 1
else
  echo ""
  echo "PASSED: Ready to run."
  exit 0
fi
