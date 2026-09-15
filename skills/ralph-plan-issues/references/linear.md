# Linear dialect

Write the graph as children of one parent issue, using the Linear MCP tools — Ralph itself only
reads, updates, and comments, so creation is always this skill's job.

**The parent is a committed batch, not a backlog.** Ralph parses *every* child, and a child in a
workflow state outside its four is fatal for the entire run. Unrefined work stays out of the parent.

**Check the team's workflow states before writing anything.** List the target team's statuses and
match Ralph's four against them — an existing `In Progress` already satisfies `in-progress` (see
**Workflow state** below) and must not be duplicated. Report which are missing and **offer** to add
them; never create them silently. Workflow states are team-wide configuration — they change every
issue and every human on that team, not just this run, and adding them usually needs an admin. There
is no MCP tool for it either, so it means `workflowStateCreate` against Linear's GraphQL API with
`LINEAR_API_KEY`. If the missing ones do not get added, stop: there is no valid state to write a
child in, and parking them in `Todo` is fatal for the entire run.

- **Description** is exactly:

  ```markdown
  ## Spec

  <prose>

  ## Acceptance criteria

  - [ ] …

  ## Findings
  ```

  Nothing else belongs there. Ralph overwrites the whole description each time the Editor revises a
  spec, so anything extra is lost on the first revision.

  The pointer to a shared tracked spec (Part 1 rule 3) is the first line inside `## Spec`. Those
  three headings are still the only ones allowed.

- **Workflow state** must be named exactly `ready`, `in-progress`, `landed`, or `needs-human` (case
  and separator are normalised, so `In Progress` is fine; `Todo` and `Done` are not). These four are
  hardcoded in Ralph as its canonical sub-issue states — they are not a per-run argument, so the
  Linear team's workflow is what changes, not the harness.
- **Dependencies are native `blocks` relations** between siblings. Never a prose "Blocked by"
  section — that is the filesystem dialect, and here it would be a second, drifting copy of an edge
  Linear already stores.
- **Identity** is the Linear identifier (`RAN-12`), not a number prefix.
- **The parent issue is for humans.** Put the index and the reading order there freely, and remember
  that no session will ever see it — Part 1 rule 3.
