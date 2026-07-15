"""Linear-backed issue storage.

Linear is the source of truth for the plan: the parent issue owns the Kanban, and its sub-issues
are the graph Ralph runs. The store reads that graph once, then mirrors Ralph's lifecycle and
Editor revisions back into Linear.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ralph.harness import Actor
from ralph.issues import Brief, Findings, SubIssueId, SubIssueState
from ralph.issues.linear import (
    LinearComment,
    LinearIssue,
    LinearIssueStore,
    LinearIssueStoreError,
    LinearStateMap,
)
from ralph.runlog import EventKind, event


def description(brief: str, findings: str = "") -> str:
    return f"## Brief\n\n{brief}\n\n## Findings\n\n{findings}\n"


class FakeLinearClient:
    def __init__(self, parent: LinearIssue) -> None:
        self.parent = parent
        self.updated_descriptions: list[tuple[str, str]] = []
        self.updated_states: list[tuple[str, str]] = []
        self.created_comments: list[tuple[str, str]] = []

    def parent_issue(self, identifier: str) -> LinearIssue:
        if identifier != self.parent.identifier:
            raise AssertionError(f"unexpected parent {identifier}")
        return self.parent

    def update_issue_description(self, issue_id: str, description: str) -> None:
        self.updated_descriptions.append((issue_id, description))
        issue = self._issue(issue_id)
        self._replace_issue(replace(issue, description=description))

    def update_issue_state(self, issue_id: str, state: str) -> None:
        self.updated_states.append((issue_id, state))
        issue = self._issue(issue_id)
        self._replace_issue(replace(issue, state=state))

    def create_comment(self, issue_id: str, body: str) -> None:
        self.created_comments.append((issue_id, body))
        issue = self._issue(issue_id)
        comment = LinearComment(id=f"comment-{len(issue.comments)}", body=body)
        self._replace_issue(replace(issue, comments=(*issue.comments, comment)))

    def _issue(self, issue_id: str) -> LinearIssue:
        if self.parent.id == issue_id:
            return self.parent
        for issue in self.parent.sub_issues:
            if issue.id == issue_id:
                return issue
        raise AssertionError(f"no issue {issue_id}")

    def _replace_issue(self, updated: LinearIssue) -> None:
        if updated.id == self.parent.id:
            self.parent = updated
            return
        children = tuple(
            updated if issue.id == updated.id else issue
            for issue in self.parent.sub_issues
        )
        self.parent = replace(self.parent, sub_issues=children)


def parent_with(*children: LinearIssue) -> LinearIssue:
    return LinearIssue(
        id="parent-id",
        identifier="RAL-1",
        title="Parent",
        description="Parent issue",
        state="ready",
        sub_issues=children,
    )


def sub_issue(
    identifier: str,
    *,
    id: str | None = None,
    title: str = "Build it",
    state: str = "ready",
    brief: str = "Do it.\n\n## Acceptance criteria\n\n- [ ] It works.",
    findings: str = "",
    blocked_by: tuple[str, ...] = (),
    comments: tuple[LinearComment, ...] = (),
) -> LinearIssue:
    return LinearIssue(
        id=id or f"{identifier}-uuid",
        identifier=identifier,
        title=title,
        description=description(brief, findings),
        state=state,
        blocked_by=blocked_by,
        comments=comments,
    )


def test_a_linear_parent_issue_loads_as_the_issue_graph() -> None:
    client = FakeLinearClient(
        parent_with(
            sub_issue("RAL-2", title="Contract"),
            sub_issue("RAL-3", title="Implementation", blocked_by=("RAL-2",)),
        )
    )

    graph, states = LinearIssueStore(parent_identifier="RAL-1", client=client).read_graph()

    assert set(graph.sub_issues) == {SubIssueId("RAL-2"), SubIssueId("RAL-3")}
    assert graph.sub_issues[SubIssueId("RAL-2")].title == "RAL-2 \u2014 Contract"
    assert graph.blockers_of(SubIssueId("RAL-3")) == frozenset({SubIssueId("RAL-2")})
    assert states == {
        SubIssueId("RAL-2"): SubIssueState.READY,
        SubIssueId("RAL-3"): SubIssueState.READY,
    }


def test_a_blocker_outside_the_parent_sub_issues_is_rejected() -> None:
    client = FakeLinearClient(parent_with(sub_issue("RAL-2", blocked_by=("RAL-99",))))

    with pytest.raises(LinearIssueStoreError, match="RAL-99"):
        LinearIssueStore(parent_identifier="RAL-1", client=client).read_graph()


def test_content_comes_from_the_linear_sub_issue_description() -> None:
    client = FakeLinearClient(
        parent_with(
            sub_issue(
                "RAL-2",
                brief="Add auth.\n\n## Acceptance criteria\n\n- [ ] Invalid credentials return 401.",
                findings="The client swallows retry errors.",
            )
        )
    )
    store = LinearIssueStore(parent_identifier="RAL-1", client=client)
    store.read_graph()

    brief, findings = store.content(SubIssueId("RAL-2"))

    assert "Invalid credentials return 401" in brief.body
    assert brief.revision == 0
    assert findings.body == "The client swallows retry errors."


async def test_record_revision_snapshots_original_then_updates_the_sub_issue_description() -> None:
    client = FakeLinearClient(parent_with(sub_issue("RAL-2", id="linear-2")))
    store = LinearIssueStore(parent_identifier="RAL-1", client=client)
    store.read_graph()

    await store.record_revision(
        SubIssueId("RAL-2"),
        Brief(body="Rewritten brief\n\n## Acceptance criteria\n\n- [ ] Still works."),
        Findings(body="Repo fact."),
    )

    assert len(client.created_comments) == 2
    assert client.created_comments[0][0] == "linear-2"
    assert "<!-- ralph:revision:0 -->" in client.created_comments[0][1]
    assert "Do it." in client.created_comments[0][1]
    assert "<!-- ralph:revision:1 -->" in client.created_comments[1][1]
    assert "Rewritten brief" in client.created_comments[1][1]

    assert client.updated_descriptions == [
        (
            "linear-2",
            "## Brief\n\nRewritten brief\n\n## Acceptance criteria\n\n- [ ] Still works.\n\n"
            "## Findings\n\nRepo fact.\n",
        )
    ]
    brief, findings = store.content(SubIssueId("RAL-2"))
    assert (brief.body, brief.revision, findings.body) == (
        "Rewritten brief\n\n## Acceptance criteria\n\n- [ ] Still works.",
        1,
        "Repo fact.",
    )


async def test_existing_revision_comments_are_continued_not_restarted() -> None:
    existing = LinearComment(id="c1", body="<!-- ralph:revision:2 -->\n## Ralph revision 2\n")
    client = FakeLinearClient(parent_with(sub_issue("RAL-2", comments=(existing,))))
    store = LinearIssueStore(parent_identifier="RAL-1", client=client)
    store.read_graph()

    await store.record_revision(
        SubIssueId("RAL-2"),
        Brief(body="v3\n\n## Acceptance criteria\n\n- [ ] x"),
        Findings(body="f3"),
    )

    assert len(client.created_comments) == 1
    assert "<!-- ralph:revision:3 -->" in client.created_comments[0][1]


async def test_terminal_events_are_mirrored_to_linear_state() -> None:
    client = FakeLinearClient(parent_with(sub_issue("RAL-2", id="linear-2")))
    state_map = LinearStateMap(landed="Done")
    store = LinearIssueStore(parent_identifier="RAL-1", client=client, states=state_map)
    store.read_graph()

    await store.write_event(
        event(SubIssueId("RAL-2"), Actor.IMPLEMENTER, EventKind.SUB_ISSUE_CLOSED, SubIssueState.LANDED)
    )

    assert client.updated_states == [("linear-2", "Done")]


async def test_final_notification_is_posted_to_the_linear_parent_issue() -> None:
    client = FakeLinearClient(parent_with(sub_issue("RAL-2", id="linear-2")))
    store = LinearIssueStore(parent_identifier="RAL-1", client=client)
    store.read_graph()

    await store.publish_notification("landed: RAL-2")

    assert len(client.created_comments) == 1
    issue_id, body = client.created_comments[0]
    assert issue_id == "parent-id"
    assert "<!-- ralph:run-notification -->" in body
    assert "## Ralph run complete" in body
    assert "Parent: RAL-1" in body
    assert "landed: RAL-2" in body


async def test_session_events_do_not_touch_linear_state() -> None:
    client = FakeLinearClient(parent_with(sub_issue("RAL-2", id="linear-2")))
    store = LinearIssueStore(parent_identifier="RAL-1", client=client)
    store.read_graph()

    await store.write_event(
        event(
            SubIssueId("RAL-2"),
            Actor.IMPLEMENTER,
            EventKind.SESSION_STARTED,
            SubIssueState.IN_PROGRESS,
        )
    )

    assert client.updated_states == []
