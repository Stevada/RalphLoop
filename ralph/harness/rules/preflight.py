"""The pre-flight. **It refuses; it does not warn.**

A warning is a thing a human reads after the damage. Every check below describes a repository the
harness would go on to damage or misjudge — a protected branch it would fast-forward, a dirty tree
it would fight, a suite it cannot run and would therefore call green, a graph it cannot read. The
correct response to each is to not start.

Every refusal is returned, not the first: a human fixing their morning should learn everything
wrong with it in one pass, not one thing per attempt.
"""

from __future__ import annotations

from ralph.harness.model.preflight import Check, Refusal, RepoFacts

_ELIDE_AFTER = 5
"""How many dirty paths to name before saying "and N more". The point of listing them is to remind
someone what they forgot to commit, and a hundred lines of `node_modules` does not do that."""


def _dirty_summary(dirty: tuple[str, ...]) -> str:
    named = ", ".join(dirty[:_ELIDE_AFTER])
    rest = len(dirty) - _ELIDE_AFTER
    return f"{named}, and {rest} more" if rest > 0 else named


def refusals(facts: RepoFacts) -> tuple[Refusal, ...]:
    """Ordered the way a human would work through them: where you are, what is in your tree, what
    the harness needs in order to run at all, and finally what it would have read."""
    found: list[Refusal] = []

    if facts.head_branch in facts.protected:
        found.append(
            Refusal(
                Check.PROTECTED_BRANCH,
                f"HEAD is on {facts.head_branch!r}, which is protected "
                f"({', '.join(sorted(facts.protected))}). Ralph fast-forwards the branch it is run "
                "from, so a run here would push a model's commits straight onto it. Cut a working "
                "branch first.",
            )
        )

    if facts.dirty:
        found.append(
            Refusal(
                Check.UNCOMMITTED_CHANGES,
                f"the working tree has uncommitted changes: {_dirty_summary(facts.dirty)}. The "
                "merge queue fast-forwards this checkout while the run is in flight; anything "
                "uncommitted in it is in the way of that, and may be lost. Commit or stash first.",
            )
        )

    if facts.pre_commit_config is not None and not facts.pre_commit_installed:
        found.append(
            Refusal(
                Check.UNINSTALLED_PRE_COMMIT_HOOKS,
                f"{facts.pre_commit_config} configures pre-commit, but no hook is installed. Every "
                "commit an Implementer makes would skip the checks this repo believes it enforces, "
                "and the harness would land the result. Run `pre-commit install`.",
            )
        )

    if facts.command_error is not None:
        found.append(Refusal(Check.MISSING_TEST_COMMAND, facts.command_error))
    elif not facts.has_test_command:
        found.append(
            Refusal(
                Check.MISSING_TEST_COMMAND,
                "the target repo has no discoverable test command. Declare a test command before "
                "starting a run.",
            )
        )

    if facts.source_error is not None:
        found.append(Refusal(Check.INVALID_ISSUE_SOURCE, facts.source_error))

    if facts.graph_error is not None:
        found.append(Refusal(Check.INVALID_ISSUE_GRAPH, facts.graph_error))

    return tuple(found)
