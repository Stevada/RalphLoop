#!/bin/bash
set -eo pipefail

# validate.sh — Pre-flight checks for a target repo before running Ralph.
# Usage: ./validate.sh /path/to/target-repo [issues-directory]

if [ -z "$1" ]; then
  echo "Usage: $0 <target-repo-path> [issues-directory]"
  echo ""
  echo "Validates that a target repo is ready for Ralph to execute issues."
  echo "If issues-directory is omitted, defaults to <target-repo>/.scratch"
  exit 1
fi

TARGET_REPO="$1"
ISSUES_DIR="${2:-$TARGET_REPO/.scratch}"

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

    # Extract blocked-by references (simplified: look for #NN patterns)
    while IFS= read -r ref; do
      ref=$(echo "$ref" | sed 's/^[[:space:]-]*//' | tr -d '\r')
      [ -z "$ref" ] && continue
      [[ "$ref" == None* ]] && continue
      [[ "$ref" == "can start"* ]] && continue

      # Strip parenthetical notes
      ref=$(echo "$ref" | sed 's/[[:space:]]*(.*//') 

      # Try to resolve numeric references
      if [[ "$ref" =~ ^#?([0-9]+) ]]; then
        num="${BASH_REMATCH[1]}"
        match=$(find "$ISSUES_DIR" -maxdepth 1 -name "${num}-*.md" 2>/dev/null | head -1)
        if [ -z "$match" ]; then
          warn "$slug: Blocked-by reference '#$num' does not resolve to a file"
        fi
      fi
    done < <(awk '/^## Blocked by/{found=1; next} found && /^- /{print substr($0,3)} found && /^#/{exit}' "$issue_file" 2>/dev/null)
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
    while IFS= read -r ref; do
      ref=$(echo "$ref" | sed 's/^[[:space:]-]*//' | tr -d '\r' | sed 's/[[:space:]]*(.*//') 
      [[ "$ref" == None* ]] && continue
      [[ "$ref" == "can start"* ]] && continue
      [ -z "$ref" ] && continue
      if [[ "$ref" =~ ^#?([0-9]+) ]]; then
        num="${BASH_REMATCH[1]}"
        match=$(find "$ISSUES_DIR" -maxdepth 1 -name "${num}-*.md" 2>/dev/null | head -1)
        if [ -n "$match" ]; then
          dep_slugs="$dep_slugs $(basename "$match" .md)"
        fi
      fi
    done < <(awk '/^## Blocked by/{found=1; next} found && /^- /{print substr($0,3)} found && /^#/{exit}' "$issue_file" 2>/dev/null)
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
