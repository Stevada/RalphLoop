# Canonical vocabulary

[`UBIQUITOUS_LANGUAGE.md`](../../UBIQUITOUS_LANGUAGE.md) is canonical for every domain term,
**including code identifiers**. A word from its "Aliases to avoid" column appearing as a class,
function, field, variable, or state name — or in prose, prompts, or docs — is a defect, not a style
nit. (No `paths` scope on purpose: this binds code *and* writing.)

- A sub-issue's terminal state is `landed`. `done` belongs to the parent issue.
- The mutex is the **merge lock**, never the "integration lock" or "queue lock".
- **Sub-issue**, never task/ticket/issue. **Findings**, never notes/hints/guidance.
- `SuiteResult.green` — never `verified`, `trusted`, or `passing`. A suite the harness runs is inside
  the **blast radius**; only CI on a clean checkout is **honest**. Holding the line in the identifiers
  is what stops the distinction eroding.
- **Blocked** refers only to Linear's `blocked by` edge. It never describes a session's state.
