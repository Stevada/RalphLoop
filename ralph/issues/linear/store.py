"""Linear-backed implementation of the issue-store seam."""

from __future__ import annotations

from dataclasses import dataclass, replace

from ralph.issues.content import Brief, Findings
from ralph.issues.graph import IssueGraph, SubIssue, SubIssueId
from ralph.issues.linear.markdown import (
    latest_revision,
    parse_content,
    render_description,
    render_revision,
    revision_comments,
)
from ralph.issues.linear.model import (
    LinearClient,
    LinearComment,
    LinearIssue,
    LinearIssueStoreError,
    LinearStateMap,
)
from ralph.issues.state import SubIssueState
from ralph.runlog import Event, EventKind


@dataclass(slots=True)
class LinearIssueStore:
    parent_identifier: str
    client: LinearClient
    states: LinearStateMap = LinearStateMap()
    _issues: dict[SubIssueId, LinearIssue] | None = None

    def read_graph(self) -> tuple[IssueGraph, dict[SubIssueId, SubIssueState]]:
        issues = self._load_once()
        if not issues:
            raise LinearIssueStoreError(
                f"Linear parent issue {self.parent_identifier!r} has no sub-issues"
            )

        siblings = set(issues)
        graph: dict[SubIssueId, SubIssue] = {}
        states: dict[SubIssueId, SubIssueState] = {}
        for id, issue in issues.items():
            blockers = frozenset(SubIssueId(blocker) for blocker in issue.blocked_by)
            missing = blockers - siblings
            if missing:
                named = ", ".join(sorted(missing))
                raise LinearIssueStoreError(
                    f"{issue.identifier} is blocked by {named}, which is not a sub-issue "
                    f"of parent {self.parent_identifier}"
                )
            parse_content(issue)
            graph[id] = SubIssue(
                id=id, title=f"{issue.identifier} \u2014 {issue.title}", blocked_by=blockers
            )
            states[id] = self.states.from_linear(issue.state)

        return IssueGraph(graph), states

    def content(self, id: SubIssueId) -> tuple[Brief, Findings]:
        issue = self._issue(id)
        brief, findings = parse_content(issue)
        return Brief(body=brief, revision=latest_revision(issue.comments)), Findings(body=findings)

    async def record_revision(self, id: SubIssueId, brief: Brief, findings: Findings) -> None:
        issue = self._issue(id)
        latest = latest_revision(issue.comments)
        if latest == 0 and not revision_comments(issue.comments):
            original_brief, original_findings = parse_content(issue)
            original = render_revision(
                0, Brief(body=original_brief), Findings(body=original_findings)
            )
            original_comment = LinearComment(id=f"ralph-revision-0-{issue.id}", body=original)
            self.client.create_comment(issue.id, original)
            issue = self._append_cached_comment(id, original_comment)

        next_revision = latest + 1
        description = render_description(brief, findings)
        self.client.update_issue_description(issue.id, description)
        comment_body = render_revision(next_revision, brief, findings)
        self.client.create_comment(issue.id, comment_body)

        issue = replace(
            issue,
            description=description,
            comments=(
                *issue.comments,
                LinearComment(id=f"ralph-revision-{next_revision}-{issue.id}", body=comment_body),
            ),
        )
        self._cache_issue(id, issue)

    async def write_event(self, e: Event) -> None:
        if e.kind is not EventKind.SUB_ISSUE_CLOSED or not isinstance(e.details, SubIssueState):
            return
        issue = self._issue(e.sub_issue)
        state = self.states.to_linear(e.details)
        self.client.update_issue_state(issue.id, state)
        self._cache_issue(e.sub_issue, replace(issue, state=state))

    def _load_once(self) -> dict[SubIssueId, LinearIssue]:
        if self._issues is None:
            parent = self.client.parent_issue(self.parent_identifier)
            self._issues = {SubIssueId(issue.identifier): issue for issue in parent.sub_issues}
        return self._issues

    def _issue(self, id: SubIssueId) -> LinearIssue:
        issues = self._load_once()
        try:
            return issues[id]
        except KeyError:
            raise LinearIssueStoreError(
                f"no sub-issue {id!r} under Linear parent {self.parent_identifier}"
            ) from None

    def _cache_issue(self, id: SubIssueId, issue: LinearIssue) -> None:
        issues = self._load_once()
        issues[id] = issue

    def _append_cached_comment(self, id: SubIssueId, comment: LinearComment) -> LinearIssue:
        issue = self._issue(id)
        updated = replace(issue, comments=(*issue.comments, comment))
        self._cache_issue(id, updated)
        return updated
