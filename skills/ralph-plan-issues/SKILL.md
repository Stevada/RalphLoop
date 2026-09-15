---
name: ralph-plan-issues
description: Cut a spec or PRD into a graph of Ralph-readable sub-issues, written as filesystem markdown under `.scratch/<phase>/issues/` or as Linear sub-issues. Use this whenever the user is planning work a Ralph run will execute — turning a PRD or phase spec into sub-issues, writing acceptance criteria an unattended Implementer can aim at, setting up a `.scratch/` phase directory, or repairing a graph Ralph refused to parse. Also use it when someone says "plan issues for Ralph", "cut this into sub-issues", or mentions a Ralph run, `.ralph.toml`, or the Planner / Implementer / Editor roles.
---

# Plan Ralph issues

You are acting as the **Planner**: the role that cuts a parent issue into a graph of sub-issues an
unattended Implementer can execute one at a time. This skill is how a Planner writes issues Ralph can
actually read.

Ralph is a harness for autonomous issue execution, and it has three actors. A **Planner** (you, here)
shapes the work. An **Implementer** — a coding CLI — takes one sub-issue per session in an isolated
git worktree, writes code and tests, and commits. An **Editor**, read-only, diagnoses a failed
session and returns a verdict. Between them sits a **merge queue** that runs the repo's own test
command on the prospective merge and fast-forwards only what passes.

Those four facts are stated here once. Part 1 draws consequences from them and does not restate them.

That shape is why this skill is strict. **Nobody is in the session.** Every ambiguity you leave
behind gets resolved by a model with no one to ask, and the resolutions that look like success are
the expensive ones.

**In: one spec or PRD. Out: the issue graph.** The input is always prose describing work to be
done — a PRD, a phase spec, a design doc. The sub-issues are what this skill *produces*, in whichever
store the user names. In filesystem mode that output is a directory of markdown files; a folder of
issues is never the input.

Ralph reads two stores. [Part 1](#part-1--rules-that-bind-both-stores) binds both, and decides
whether a run works at all. Each store's own form is a separate file under `references/`, and you
read **only the one the user chose**. The split is not editorial: a dialect is a file format, while a
Part 1 rule is what the harness can execute.

## Step 0 — ask where the issues should be written

**Always ask the user, every time.** Do not infer it from the repo's contents, and do not carry it
over from a previous session:

> Should these issues be written as **filesystem markdown** in the target repo, or into **Linear**?

Do not proceed until they answer. Everything downstream branches once, here.

## Step 1 — read the source material, and the repo it targets

Read the spec or PRD the user named, in full, before proposing a graph.

Then read the target repo, because two of the Part 1 rules are unanswerable without it:

- **Its command descriptor, `.ralph.toml` at the repo root.** `[commands].test` is the command the
  merge queue will run, and it is the *only* thing that can decide an acceptance criterion — see
  [rule 2](#2-every-criterion-must-be-decidable-by-the-repos-test-command-inside-a-worktree). Read the
  actual command; do not assume `pytest` or `npm test`. **If the repo has no `.ralph.toml`, no
  criterion you write is decidable and Ralph will refuse the run outright.** Say so now, and offer to
  write one — it is a few lines, and it is cheaper to add before the graph than after.
- **Its context file and vocabulary** — `CLAUDE.md` or `AGENTS.md`, and a `UBIQUITOUS_LANGUAGE.md` or
  `CONTEXT.md` if it has one. Name every concept the way the repo names it. A sub-issue that invents
  vocabulary sends the Implementer looking for something that does not exist.

**A spec a session needs must be a tracked file in the repo, and every sub-issue that leans on it
must say so.** Sub-issues routinely lean on the document they were cut from, and a session can only
see two things — see [rule 3](#3-a-session-sees-exactly-two-things). If the source document lives
somewhere a worktree cannot (an untracked `.scratch/`, a Linear description, a chat), move it to a
stable home and commit it — `docs/specs/<phase>.md` beside a `PRD.md` keeps each phase's spec
findable without disturbing `.scratch/`. Then point at it from each sub-issue that needs it.

Inlining what the sub-issues need is the fallback when committing is genuinely not an option, and it
is worse: the same knowledge in N descriptions drifts, and Ralph overwrites a whole description each
time the Editor revises a spec, so inlined material disappears mid-run.

## Step 2 — propose the graph before writing it

Show the user the sub-issue list and its dependency edges, and stop. A graph is cheap to redraw in
conversation and expensive to redraw once it is twenty files and a set of Linear relations.

## Step 3 — write it in the dialect they chose

Read exactly one, now that Step 0 has been answered:

- **filesystem** → `references/filesystem.md`
- **Linear** → `references/linear.md`

## Step 4 — make the harness read back what you wrote

You have just written the input to a parser that treats every ambiguity as fatal. Do not hand it over
unverified.

If the `ralph` CLI is available here — check with `ralph --help`, or `uv run ralph --help` from a
Ralph source checkout — run it against what you wrote:

```bash
ralph run --dry-run <repo> [issue-source]   # parse the graph, print the build order
ralph validate <repo> [issue-source]        # every pre-flight refusal, not just the first
```

`--dry-run` parses every sub-issue, resolves every edge, proves the graph is acyclic, and prints the
build order in waves. **No session is opened, so it costs nothing but a second** — and it is the same
code path the real run uses, so a graph it accepts is a graph the run accepts.

`validate` returns up to six refusals. Two of them are yours: `invalid-issue-source` (the store could
not be read) and `invalid-issue-graph` (it read, but the graph is malformed — a cycle, a spec with no
acceptance criteria). The other four — `protected-branch`, `uncommitted-changes`,
`uninstalled-pre-commit-hooks`, `missing-test-command` — describe the machine the run will start on,
not your planning. Fix yours; report the rest.

Linear mode needs `--issue-mode linear` with the parent identifier as the issue source, and
`LINEAR_API_KEY` in the target repo's `.env`. Filesystem is the default.

If `ralph` is not installed here, say that plainly rather than implying the graph was checked, and
walk the Part 1 rules by hand instead.

---

## Part 1 — rules that bind both stores

These decide whether a run works at all. Every one of them traces to something the harness does.

### 1. `## Acceptance criteria` is mandatory

Its absence is a fatal parse error in both stores. An unattended Implementer has nothing else to aim
at.

### 2. Every criterion must be decidable by the repo's test command, inside a worktree

That run, not a model's opinion, is what lands the work. A criterion the suite cannot express is
invisible to the harness — the Implementer will read it, believe it, and self-assess against it,
which is the same as nobody checking.

- Good: `A Listing older than 30 days never produces a Match`
- Bad: `The code is clean and idiomatic` — nothing decides this
- Bad: `` `Any` does not appear in the codebase `` — repo-wide, and still true or false long after
  this sub-issue lands. Make it a lint rule the sub-issue installs, then the criterion is that the
  rule runs in the test command.

### 3. A session sees exactly two things

Its **own spec** (plus any findings), and the **repo checkout in a worktree**. That is the entire
channel. Three consequences a Planner keeps tripping over:

- **Nothing on the parent issue reaches a session.** Not an index, not a reading order, not a phase
  overview, not a `PRD.md` sitting beside `issues/`. Write those for the humans, and never rely on
  them.
- **Only tracked files exist.** `git worktree add` does not carry ignored or untracked files, so a
  sub-issue may not depend on anything gitignored — commonly `.scratch/` itself. If a document is
  load-bearing for the work, commit it.
- **Tracked is not the same as found.** A session aims at its own spec; it will not go looking for a
  phase spec nobody told it about. Every sub-issue leaning on a shared document names that document
  **in its own spec**, and names the sections it needs — `read §4 Matching and §6 Lifecycle`, not a
  bare "see the spec", which is ambient and gets skipped.

Put that pointer in the sub-issue, never in the repo's context file instead. A standing line in
`CLAUDE.md` naming one phase's spec is right until the next phase exists and misleading forever
after, whereas a sub-issue belongs to exactly one phase and is read once — its pointer cannot rot.

The test: if a fact matters to the work, it is in that sub-issue's spec or in a tracked file the
sub-issue points at. There is no third place.

### 4. One sub-issue is one vertical slice, and it produces commits

One session, one worktree, one merge, one fresh context. If a sub-issue needs a second session to
finish, it was two sub-issues.

It also has to *change something*. A session that ends with zero commits is classified as an
**impasse**, because the harness cannot distinguish "there was nothing to do" from "I did nothing."
So never write a sub-issue whose honest outcome is a no-op: no "confirm X still works", no "review
Y". If you want that checked, the sub-issue is *"add a test asserting X"*, and the commit is the test.

### 5. Siblings that land together must not fight over the same files

Sub-issues with no edge between them run concurrently, and each lands by rebasing onto whatever the
integration branch has become. Two slices rewriting the same module put whichever one arrives second
into conflict: the harness resumes that session once to resolve it mechanically, and if that fails
the sub-issue is `integration-failed`. The work was fine; the graph was wrong, and you paid a model
to find that out.

Slice along seams, not across them. Where two sub-issues genuinely must touch one file, an edge
between them is the cheap fix: it costs a little parallelism and buys a merge that works.

### 6. `landed` is written by the merge queue

Never by a model, and never by a Planner to describe code that already exists — a sub-issue that
claims to have landed when nothing merged has lied to the harness about the state of the repository.

The one exception is [rule 7](#7-operational-work-is-written-as-needs-human)'s operational
sub-issues: they produce no commits, so no merge queue can ever mark them, and a human recording that
the work is genuinely done is the only way their dependents ever run.

### 7. Operational work is written as `needs-human`

Registering a domain, buying a phone number, verifying an account, clicking through a dashboard —
there is no code path, so a session cannot finish it. Write these sub-issues in the `needs-human`
state from the start, and say in the spec that this is what they are.

Be explicit with the user about what that costs. `needs-human` is transitive: a sub-issue runs only
once every sub-issue it is blocked by has **landed**, so while an operational sub-issue sits there,
everything behind it is permanently drained for that run. The human does the work, sets it to
`landed` by hand, and the dependents become eligible on the next run.

Prefer a graph where operational work blocks as little as possible. If it blocks nothing, leave it
out of the graph entirely and track it as ordinary intake.

### 8. The graph is acyclic, and every blocker is a sibling

A cycle is fatal. So is a blocker outside the sub-issue set — a fatal error, not a warning.

### 9. `## Findings` belongs to the Editor

It is where a read-only Editor writes what it learned about the repo after a failed session — *`add()`
is in `calculator.py`, not `math.py`*. A Planner writing into it is pre-filling a diagnosis of
something that has not happened. Leave it empty (Linear keeps the heading; filesystem omits it), and
put everything you know into the spec, where it belongs.
