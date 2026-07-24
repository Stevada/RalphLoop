"""Linear GraphQL client for Ralph's issue-store adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ralph.issues.linear.model import LinearComment, LinearIssue, normalize_state

GRAPHQL_ENDPOINT = "https://api.linear.app/graphql"

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


class LinearApiError(RuntimeError):
    """The Linear GraphQL API could not satisfy the request."""


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
                normalize_state(_string(node, "name")): _string(node, "id") for node in nodes
            }
        try:
            return self._state_ids[normalize_state(state)]
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

