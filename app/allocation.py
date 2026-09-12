"""Fixed-point largest-remainder allocation on 0.001 kWh units.

Every quantity in this module is an integer count of "milliunits"
(1 milliunit = 0.001 kWh). No binary floating point is ever involved,
so the smallest unit can never be lost to rounding.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

MILLIUNITS_PER_KWH = 1000


class ZeroTotalWeightError(Exception):
    """Raised when the difference is non-zero but every interval weight is zero."""


def summarize_intervals(
    energies: Iterable[tuple[str, int]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Aggregate ``(interval, milliunits)`` pairs into signed totals and weights.

    An interval's weight is the sum of the *absolute* energy of every branch
    in that interval, so positive and negative branch readings that cancel
    each other still contribute their magnitude to the weight.
    """
    totals: dict[str, int] = {}
    weights: dict[str, int] = {}
    for interval, energy in energies:
        totals[interval] = totals.get(interval, 0) + energy
        weights[interval] = weights.get(interval, 0) + abs(energy)
    return totals, weights


def to_milliunits(value: Decimal) -> int:
    """Convert a Decimal kWh value (already validated to <= 3 decimals) to milliunits."""
    return int(value * MILLIUNITS_PER_KWH)


def format_milliunits(milliunits: int) -> str:
    """Render milliunits as a kWh string with exactly three decimal places."""
    sign = "-" if milliunits < 0 else ""
    magnitude = abs(milliunits)
    return f"{sign}{magnitude // MILLIUNITS_PER_KWH}.{magnitude % MILLIUNITS_PER_KWH:03d}"


def allocate_difference(difference: int, weights: dict[str, int]) -> dict[str, int]:
    """Distribute ``difference`` milliunits across intervals proportionally to ``weights``.

    Largest remainder method in fixed point:

    * each interval's exact share ``difference * weight / total_weight`` is
      truncated toward zero;
    * the leftover milliunits are handed out one per interval, ordered by
      descending absolute remainder, ties broken by ascending interval id
      (lexicographic);
    * a negative difference hands out negative milliunits in the same order.

    The returned mapping always sums exactly to ``difference``.
    Raises :class:`ZeroTotalWeightError` if the difference is non-zero while
    the total weight is zero.
    """
    if difference == 0:
        return {interval: 0 for interval in weights}

    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ZeroTotalWeightError(
            "total absolute branch energy is zero; "
            "cannot allocate a non-zero difference"
        )

    sign = 1 if difference > 0 else -1
    shares: dict[str, int] = {}
    remainders: dict[str, int] = {}
    truncated_sum = 0
    for interval, weight in weights.items():
        magnitude = abs(difference) * weight
        quotient, remainder = divmod(magnitude, total_weight)
        shares[interval] = sign * quotient
        remainders[interval] = remainder
        truncated_sum += sign * quotient

    leftover = abs(difference - truncated_sum)
    order = sorted(weights, key=lambda interval: (-remainders[interval], interval))
    for interval in order[:leftover]:
        shares[interval] += sign
    return shares


def allocate_to_branches(allocated: int, branch_energies: dict[str, int]) -> dict[str, int]:
    """Second-level allocation: spread an interval's ``allocated`` milliunits
    across its branches proportionally to each branch's *absolute* energy.

    Uses the same fixed-point largest-remainder rules as
    :func:`allocate_difference`, with remainder ties resolved by ascending
    branch id (lexicographic). The result always sums exactly to
    ``allocated``. A non-zero ``allocated`` implies the interval had non-zero
    weight, so at least one branch weight is positive and
    :class:`ZeroTotalWeightError` cannot occur here.
    """
    weights = {branch: abs(energy) for branch, energy in branch_energies.items()}
    return allocate_difference(allocated, weights)
