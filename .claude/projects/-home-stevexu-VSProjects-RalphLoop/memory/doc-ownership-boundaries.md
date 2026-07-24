---
name: doc-ownership-boundaries
description: How the Ralph docs are organized — a reference DAG with single-owner rationale (design.md)
metadata:
  type: feedback
---

The docs form an **acyclic reference DAG**: each points only toward more detail or toward a shared
leaf, so there is always a valid reading order and no reference cycles. Two entry points, two shared
leaves everything points down to.

**Entry points (roots):**
- **README.md** — human entry / reading index. May link anything.
- **CLAUDE.md** — agent entry (auto-loaded). What's true *now* + how to work: status, commands, env.
  Coding rules are NOT inline here — they auto-load from `.claude/rules/` (see below).

**Shared leaves (sinks — everything points down to them; they point at nothing):**
- **UBIQUITOUS_LANGUAGE.md** — what each term *means*. Definitions only, a sentence or two, never a
  justification. **Points at nothing** — the vocabulary bottoms out here.
- **docs/design.md** — the single home for *why*: every design argument, bet, and rationale. (Was
  `docs/prd.md`; renamed and re-cast when the build finished, §9 bash-traps and §10 build-order
  pruned as spent. Points only down to UL for terms.)

**Mid-layer detail (point down to the two leaves, never sideways/up):**
- **docs/architecture.md** — *how it's built*, a **map not a replica**: layers, where each concept
  lives, seams, cross-file invariants. Names types/rules + links their module rather than inlining
  code (code + mypy own signatures). Rationale that duplicates design.md is a one-line pointer.
- **docs/cli-metering.md** — empirical per-CLI facts (Codex rollout format, Copilot log gotchas,
  56.5k→8.4k). External, hard-won. A leaf under architecture; does NOT point back up to it.
- **code comments** — only local, non-obvious *why* no name/type can carry (e.g. async ordering).

**Coding rules live in `.claude/rules/*.md`** (auto-loaded, no `@import` needed), two files, both
always-on: `coding-standards.md` (how to work + what to build — behavioural rules merged with
type/DRY/SSOT/layering/failure discipline) and `canonical-vocabulary.md` (terms — binds code *and*
prose). **Rules files must not reference each other** (they all load into context together, so a
cross-link is noise) — they may only point down to `docs/`.

**Why:** Started 2026-07-15 when the user flagged scheduler.py's heavy comments made the code *look*
complex. Root cause was ~10 design arguments each restated 3–5× across the docs + code (a §3 SSOT
violation in prose). Fix: centralize rationale in one doc (design.md), point everything else at it,
and make the reference graph acyclic.

**How to apply:** Keep rationale in design.md and *point* to it; never re-argue it in the detail docs
or in code comments. When editing, don't add a reference that points from a leaf back up (that
recreates a cycle) — UL and design.md point at nothing. See [[scheduler-comment-density]] if created.
