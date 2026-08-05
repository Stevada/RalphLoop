"""What a session cost, in tokens.

Telemetry only — nothing in the harness is gated on any of it. It lives in the core rather than in
an adapter because all three concrete adapters report it and every layer above them carries it, and
a number that three vendors each shape differently needs one shape they are all translated into.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import reduce


def _add(left: int | None, right: int | None) -> int | None:
    """Unknown taints. Adding a bucket nobody reported to one somebody did would produce a number
    that looks like a measurement and is not one."""
    return None if left is None or right is None else left + right


@dataclass(frozen=True, slots=True)
class TokenConsumption:
    """The three buckets and the total they sum to.

    `consumed_tokens` is authoritative and always known; a bucket is `None` when the vendor reported
    a total and no breakdown of it.
    """

    consumed_tokens: int
    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    output_tokens: int | None = None

    @classmethod
    def split(cls, input: int, cache_read: int, output: int) -> TokenConsumption:
        """A vendor's components, translated. The total is derived, never read alongside them: two
        sources for one number is one source too many."""
        return cls(
            consumed_tokens=input + cache_read + output,
            input_tokens=input,
            cache_read_tokens=cache_read,
            output_tokens=output,
        )

    @classmethod
    def total_only(cls, consumed_tokens: int) -> TokenConsumption:
        """All a vendor that reports no components can honestly say."""
        return cls(consumed_tokens=consumed_tokens)

    def __add__(self, other: TokenConsumption) -> TokenConsumption:
        return TokenConsumption(
            consumed_tokens=self.consumed_tokens + other.consumed_tokens,
            input_tokens=_add(self.input_tokens, other.input_tokens),
            cache_read_tokens=_add(self.cache_read_tokens, other.cache_read_tokens),
            output_tokens=_add(self.output_tokens, other.output_tokens),
        )


NOTHING = TokenConsumption.split(input=0, cache_read=0, output=0)
"""A session that consumed nothing — the stand-in agents' whole bill, and the sum of no sessions."""


def total(records: Iterable[TokenConsumption]) -> TokenConsumption:
    return reduce(lambda left, right: left + right, records, NOTHING)
