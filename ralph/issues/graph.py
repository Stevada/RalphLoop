"""The issue graph: structure, frozen for the life of a run.

The Editor may never add, remove, or re-link a sub-issue. That invariant is enforced here, by
the type, rather than by a rule someone has to remember.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import NewType

SubIssueId = NewType("SubIssueId", str)


class GraphError(ValueError):
    """The graph is not a DAG over sub-issues that exist."""


@dataclass(frozen=True, slots=True)
class SubIssue:
    id: SubIssueId
    blocked_by: frozenset[SubIssueId]


@dataclass(frozen=True, slots=True)
class IssueGraph:
    """Read once, at run start. Never mutated."""

    sub_issues: Mapping[SubIssueId, SubIssue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sub_issues", MappingProxyType(dict(self.sub_issues)))
        self._reject_mislabelled_keys()
        self._reject_dangling_edges()
        self._reject_cycles()

    def blockers_of(self, id: SubIssueId) -> frozenset[SubIssueId]:
        return self.sub_issues[id].blocked_by

    def transitively_blocked_by(self, id: SubIssueId) -> frozenset[SubIssueId]:
        """Every sub-issue that must land before `id` can. Excludes `id` itself."""
        seen: set[SubIssueId] = set()
        frontier = list(self.blockers_of(id))
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(self.sub_issues[current].blocked_by)
        return frozenset(seen)

    def _reject_mislabelled_keys(self) -> None:
        for key, sub in self.sub_issues.items():
            if key != sub.id:
                raise GraphError(f"sub-issue keyed as {key!r} but its id is {sub.id!r}")

    def _reject_dangling_edges(self) -> None:
        for sub in self.sub_issues.values():
            for blocker in sorted(sub.blocked_by):
                if blocker not in self.sub_issues:
                    raise GraphError(
                        f"{sub.id!r} is blocked by {blocker!r}, which is not a sub-issue"
                    )

    def _reject_cycles(self) -> None:
        white, grey, black = 0, 1, 2
        colour: dict[SubIssueId, int] = dict.fromkeys(self.sub_issues, white)

        for root in self.sub_issues:
            if colour[root] != white:
                continue
            colour[root] = grey
            path: list[SubIssueId] = [root]
            stack: list[tuple[SubIssueId, Iterator[SubIssueId]]] = [(root, self._edges(root))]

            while stack:
                node, edges = stack[-1]
                nxt = next(edges, None)
                if nxt is None:
                    colour[node] = black
                    stack.pop()
                    path.pop()
                elif colour[nxt] == grey:
                    cycle = [*path[path.index(nxt) :], nxt]
                    raise GraphError("cycle in the issue graph: " + " -> ".join(cycle))
                elif colour[nxt] == white:
                    colour[nxt] = grey
                    path.append(nxt)
                    stack.append((nxt, self._edges(nxt)))

    def _edges(self, id: SubIssueId) -> Iterator[SubIssueId]:
        return iter(sorted(self.sub_issues[id].blocked_by))
