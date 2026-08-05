"""The three token buckets.

The buckets exist to answer one question a bare total cannot: *what did this actually cost?* A
cache read is roughly a tenth the price of fresh prompt, so a run reported as "50,000 tokens" can
differ tenfold in money depending on a split that the old single number threw away. Everything
below defends that the split stays honest — including when a vendor does not report one.
"""

from __future__ import annotations

from ralph.harness import NOTHING, TokenConsumption, total


def test_the_buckets_sum_to_the_total() -> None:
    """`consumed_tokens` is derived from the buckets, never read alongside them. Two sources for one
    number is how a total and its parts drift apart."""
    c = TokenConsumption.split(input=30_000, cache_read=15_000, output=5_000)

    assert c.consumed_tokens == 50_000


def test_a_vendor_that_reports_only_a_total_gets_no_invented_buckets() -> None:
    """Zeros would read as a session that consumed no prompt and generated nothing — a measurement,
    and a false one. `None` says the only true thing: nobody reported it."""
    c = TokenConsumption.total_only(1_234)

    assert c.consumed_tokens == 1_234
    assert (c.input_tokens, c.cache_read_tokens, c.output_tokens) == (None, None, None)


def test_adding_two_known_splits_adds_every_bucket() -> None:
    first = TokenConsumption.split(input=10, cache_read=20, output=30)
    second = TokenConsumption.split(input=1, cache_read=2, output=3)

    assert first + second == TokenConsumption.split(input=11, cache_read=22, output=33)


def test_one_session_without_a_breakdown_taints_the_buckets_but_not_the_total() -> None:
    """The property that makes the sum trustworthy. A run where one session reported only a total
    cannot report a bucket sum — the number would be missing that session's share and would look
    exactly like a real one. The total survives, because that part *is* known.
    """
    known = TokenConsumption.split(input=10, cache_read=20, output=30)

    summed = known + TokenConsumption.total_only(5)

    assert summed.consumed_tokens == 65
    assert (summed.input_tokens, summed.cache_read_tokens, summed.output_tokens) == (
        None,
        None,
        None,
    )


def test_the_sum_of_no_sessions_is_nothing() -> None:
    assert total([]) == NOTHING
    assert NOTHING.consumed_tokens == 0
    assert NOTHING.input_tokens == 0  # nothing is *measured* at zero, not unknown


def test_totalling_is_the_same_as_adding_in_sequence() -> None:
    records = [
        TokenConsumption.split(input=1, cache_read=2, output=3),
        TokenConsumption.split(input=10, cache_read=20, output=30),
        TokenConsumption.total_only(7),
    ]

    assert total(records) == records[0] + records[1] + records[2]
    assert total(records).consumed_tokens == 73
