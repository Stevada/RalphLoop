# Filesystem dialect

Write the graph as markdown files under `<repo>/.scratch/<phase>/issues/`, creating the directory.
This directory is an output of this skill, not an input to it.

```markdown
# Matching, and the Matches list

Status: ready

## What to build

<prose: what this slice is, and the decisions the Implementer should not have to re-make>

## Acceptance criteria

- [ ] A `seeking` Listing never produces a Match
- [ ] A Listing older than 30 days never produces a Match

## Blocked by

- 05 — Onboarding: Expectation and notification fields
- 09 — Normalization: Raw Post to Listing
```

- **Filename** is `NN-slug.md`, numbered from `01`. The numeric prefix is the sub-issue's identity.
- **Every `*.md` file in `issues/` is parsed as a sub-issue.** An index, a README, or a stray note in
  that directory is a fatal parse error. Put them one level up, in `.scratch/<phase>/`.
- **`Status:`** sits on its own line as a single bare word: `ready` | `in-progress` | `landed` |
  `needs-human`. Any other value is fatal. A Planner writes `ready`, and nothing else.
- **`## Blocked by`** takes bullets naming sibling numbers — `#02`, `02`, and `02-slug.md` all name
  sub-issue `02` — or a bullet starting with `None`. A bullet with no number in it is fatal.
- **`## Acceptance criteria`, `## Blocked by`, and `## Findings` are the only headings the parser
  knows.** The whole file is the spec, so everything else — `## What to build`, `## Context`,
  whatever the work needs — is yours to shape, and a section runs until the next `##`.
- **Never create `revisions/` or `consumption.jsonl`** in `issues/`. The harness owns both.
- **One phase directory in flight.** With several `.scratch/*/issues` present, Ralph refuses to guess
  and the user must name one on the command line.

A `PRD.md` beside `issues/` is not injected into any prompt, and `.scratch/` is typically gitignored,
so it is reachable only if it is tracked — Part 1 rule 3.
