"""Linear-backed issue storage.

The parent issue is the Kanban. Its sub-issues are the graph Ralph runs. The current brief and
findings live in each Linear sub-issue's description; Editor revision history is append-only Ralph
comments on that same sub-issue.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol, cast

from ralph.issues.content import Brief, Findings
from ralph.issues.graph import IssueGraph, SubIssue, SubIssueId
from ralph.issues.state import SubIssueState
from ralph.runlog import Event, EventKind

GRAPHQL_ENDPOINT = "https://api.linear.app/graphql"
BRIEF_HEADING = "## Brief"
FINDINGS_HEADING = "## Findings"
ACCEPTANCE_HEADING = "## Acceptance criteria"
REVISION = re.compile(r"<!--\s*ralph:revision:(\d+)\s*-->")

_PARENT_QUERY = """
query RalphParentIssue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    state { name }
    children(first: 100) {
      nodes {
        id
        identifier
        title
        description
        state { name }
        inverseRelations(first: 100) {
          nodes {
            type
            issue { identifier }
          }
        }
        comments(first: 100) {
          nodes {
            id
            body
          }
        }
      }
    }
  }
}
"""

_UPDATE_DESCRIPTION = """
mutation RalphUpdateIssueDescription($id: String!, $description: String!) {
  issueUpdate(id: $id, input: {description: $description}) {
    success
  }
}
"""

_CREATE_COMMENT = """
mutation RalphCreateComment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
  }
}
"""

_WORKFLOW_STATES = """
query RalphWorkflowStates {
  workflowStates(first: 250) {
    nodes {
      id
      name
    }
  }
}
"""

_UPDATE_STATE = """
mutation RalphUpdateIssueState($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: {stateId: $stateId}) {
    success
  }
}
"""


class LinearIssueStoreError(ValueError):
    """Linear did not contain the parent/sub-issue shape Ralph requires."""


class LinearApiError(RuntimeError):
    """The Linear GraphQL API could not satisfy the request."""


@dataclass(frozen=True, slots=True)
class LinearComment:
    id: str
    body: str


@dataclass(frozen=True, slots=True)
class LinearIssue:
    id: str
    identifier: str
    title: str
    description: str
    state: str
    blocked_by: tuple[str, ...] = ()
    comments: tuple[LinearComment, ...] = ()
    sub_issues: tuple[LinearIssue, ...] = ()


class LinearClient(Protocol):
    def parent_issue(self, identifier: str) -> LinearIssue: ...

    def update_issue_description(self, issue_id: str, description: str) -> None: ...

    def update_issue_state(self, issue_id: str, state: str) -> None: ...

    def create_comment(self, issue_id: str, body: str) -> None: ...


@dataclass(frozen=True, slots=True)
class LinearStateMap:
    """Linear workflow state names for Ralph's four sub-issue states."""

    ready: str = "ready"
    in_progress: str = "in-progress"
    landed: str = "landed"
    needs_human: str = "needs-human"

    def from_linear(self, raw: str) -> SubIssueState:
        normalized = _normalize_state(raw)
        for state, name in self._pairs().items():
            if normalized == _normalize_state(name):
                return state
        raise LinearIssueStoreError(
            f"Linear state {raw!r} is not a Ralph sub-issue state. "
            f"Expected: {', '.join(self._pairs().values())}."
        )

    def to_linear(self, state: SubIssueState) -> str:
        return self._pairs()[state]

    def _pairs(self) -> Mapping[SubIssueState, str]:
        return {
            SubIssueState.READY: self.ready,
            SubIssueState.IN_PROGRESS: self.in_progress,
            SubIssueState.LANDED: self.landed,
            SubIssueState.NEEDS_HUMAN: self.needs_human,
        }


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
            _parse_content(issue)
            graph[id] = SubIssue(id=id, title=f"{issue.identifier} \u2014 {issue.title}", blocked_by=blockers)
            states[id] = self.states.from_linear(issue.state)

        return IssueGraph(graph), states

    def content(self, id: SubIssueId) -> tuple[Brief, Findings]:
        issue = self._issue(id)
        brief, findings = _parse_content(issue)
        return Brief(body=brief, revision=_latest_revision(issue.comments)), Findings(body=findings)

    async def record_revision(self, id: SubIssueId, brief: Brief, findings: Findings) -> None:
        issue = self._issue(id)
        latest = _latest_revision(issue.comments)
        if latest == 0 and not _revision_comments(issue.comments):
            original_brief, original_findings = _parse_content(issue)
            original = _render_revision(
                0, Brief(body=original_brief), Findings(body=original_findings)
            )
            original_comment = LinearComment(id=f"ralph-revision-0-{issue.id}", body=original)
            self.client.create_comment(issue.id, original)
            issue = self._append_cached_comment(id, original_comment)

        next_revision = latest + 1
        description = _render_description(brief, findings)
        self.client.update_issue_description(issue.id, description)
        comment_body = _render_revision(next_revision, brief, findings)
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


@dataclass(slots=True)
class LinearGraphQLClient:
    api_key: str
    endpoint: str = GRAPHQL_ENDPOINT
    _state_ids: dict[str, str] | None = None

    def parent_issue(self, identifier: str) -> LinearIssue:
        data = self._graphql(_PARENT_QUERY, {"id": identifier})
        issue = data.get("issue")
        if issue is None:
            raise LinearApiError(f"Linear issue {identifier!r} was not found")
        if not isinstance(issue, Mapping):
            raise LinearApiError(f"Linear issue {identifier!r} was not an object")
        return _issue_from_graphql(issue)

    def update_issue_description(self, issue_id: str, description: str) -> None:
        self._graphql(_UPDATE_DESCRIPTION, {"id": issue_id, "description": description})

    def update_issue_state(self, issue_id: str, state: str) -> None:
        state_id = self._state_id(state)
        self._graphql(_UPDATE_STATE, {"id": issue_id, "stateId": state_id})

    def create_comment(self, issue_id: str, body: str) -> None:
        self._graphql(_CREATE_COMMENT, {"issueId": issue_id, "body": body})

    def _state_id(self, state: str) -> str:
        if self._state_ids is None:
            data = self._graphql(_WORKFLOW_STATES, {})
            nodes = _nodes(data.get("workflowStates"))
            self._state_ids = {
                _normalize_state(_string(node, "name")): _string(node, "id") for node in nodes
            }
        try:
            return self._state_ids[_normalize_state(state)]
        except KeyError:
            raise LinearApiError(f"no Linear workflow state named {state!r}") from None

    def _graphql(self, query: str, variables: Mapping[str, object]) -> Mapping[str, object]:
        body = json.dumps({"query": query, "variables": dict(variables)}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = cast(object, json.loads(response.read().decode()))
        except urllib.error.URLError as exc:
            raise LinearApiError(f"Linear API request failed: {exc}") from exc

        if not isinstance(payload, Mapping):
            raise LinearApiError("Linear API response was not a JSON object")
        if payload.get("errors"):
            errors = payload["errors"]
            if isinstance(errors, list):
                messages = "; ".join(_error_message(error) for error in errors)
            else:
                messages = str(errors)
            raise LinearApiError(f"Linear API returned errors: {messages}")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise LinearApiError("Linear API response had no data object")
        return data


def _issue_from_graphql(raw: Mapping[str, object]) -> LinearIssue:
    children = tuple(_issue_from_graphql(child) for child in _nodes(raw.get("children")))
    comments = tuple(
        LinearComment(id=_string(comment, "id"), body=_string(comment, "body"))
        for comment in _nodes(raw.get("comments"))
    )
    state = _mapping(raw.get("state"))
    return LinearIssue(
        id=_string(raw, "id"),
        identifier=_string(raw, "identifier"),
        title=_string(raw, "title"),
        description=_string(raw, "description"),
        state=_string(state, "name"),
        blocked_by=_blocked_by(raw),
        comments=comments,
        sub_issues=children,
    )


def _blocked_by(raw: Mapping[str, object]) -> tuple[str, ...]:
    blockers: set[str] = set()
    # Linear stores "A blocks B" as a `blocks` relation from A to B. A child issue B can therefore
    # learn its blockers from inverse relations where the source issue blocks it.
    for relation in _nodes(raw.get("inverseRelations")):
        if relation.get("type") == "blocks":
            issue = _mapping(relation.get("issue"))
            identifier = _string(issue, "identifier")
            if identifier:
                blockers.add(identifier)
    return tuple(sorted(blockers))


def _nodes(connection: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(connection, Mapping):
        return ()
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return ()
    return tuple(node for node in nodes if isinstance(node, Mapping))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    return "" if value is None else str(value)


def _error_message(error: object) -> str:
    if isinstance(error, Mapping):
        return _string(error, "message") or str(error)
    return str(error)


def _parse_content(issue: LinearIssue) -> tuple[str, str]:
    body = issue.description
    brief = _brief_section(body)
    findings = _section(body, FINDINGS_HEADING)
    if not brief:
        brief = _without_findings(body).strip()
    if ACCEPTANCE_HEADING not in brief:
        raise LinearIssueStoreError(
            f"{issue.identifier} has no `{ACCEPTANCE_HEADING}` in its Linear description"
        )
    return brief.strip(), findings.strip()


def _brief_section(body: str) -> str:
    start = body.find(BRIEF_HEADING)
    if start == -1:
        return ""
    rest = body[start + len(BRIEF_HEADING) :]
    end = rest.find(FINDINGS_HEADING)
    return rest if end == -1 else rest[:end]


def _section(body: str, heading: str) -> str:
    start = body.find(heading)
    if start == -1:
        return ""
    rest = body[start + len(heading) :]
    match = re.search(r"\n##\s+", rest)
    return rest if match is None else rest[: match.start()]


def _without_findings(body: str) -> str:
    start = body.find(FINDINGS_HEADING)
    return body if start == -1 else body[:start]


def _render_description(brief: Brief, findings: Findings) -> str:
    return f"{BRIEF_HEADING}\n\n{brief.body.strip()}\n\n{FINDINGS_HEADING}\n\n{findings.body.strip()}\n"


def _render_revision(revision: int, brief: Brief, findings: Findings) -> str:
    return (
        f"<!-- ralph:revision:{revision} -->\n"
        f"## Ralph revision {revision}\n\n"
        "### Brief\n\n"
        f"{brief.body.strip()}\n\n"
        "### Findings\n\n"
        f"{findings.body.strip()}\n"
    )


def _revision_comments(comments: tuple[LinearComment, ...]) -> tuple[LinearComment, ...]:
    return tuple(comment for comment in comments if REVISION.search(comment.body))


def _latest_revision(comments: tuple[LinearComment, ...]) -> int:
    versions = [
        int(match.group(1)) for comment in comments if (match := REVISION.search(comment.body))
    ]
    return max(versions) if versions else 0


def _normalize_state(raw: str) -> str:
    return re.sub(r"[\s_-]+", "-", raw.strip().casefold())
