"""Fixed-point largest-remainder allocation on 0.001 kWh units.

Every quantity in this module is an integer count of "milliunits"
(1 milliunit = 0.001 kWh). No binary floating point is ever involved,
so the smallest unit can never be lost to rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

MILLIUNITS_PER_KWH = 1000


class ZeroTotalWeightError(Exception):
    """Raised when the difference is non-zero but every interval weight is zero."""


@dataclass(frozen=True)
class AllocationTraceEntry:
    """One key's calculation record inside a largest-remainder allocation.

    All quantities are the exact integers used by the fixed-point computation:

    * ``weight``: the key's absolute-energy weight in milliunits;
    * ``truncated_share``: the share after toward-zero truncation, in
      milliunits;
    * ``remainder``: the fixed-point remainder that ranked the key when the
      leftover milliunits were handed out;
    * ``leftover_units``: signed milliunits added to this key in the leftover
      pass (``+1``/``-1``/``0``; negative differences hand out negative units);
    * ``share``: the final allocated share in milliunits
      (``truncated_share + leftover_units``).
    """

    key: str
    weight: int
    truncated_share: int
    remainder: int
    leftover_units: int
    share: int


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
    shares, _ = allocate_difference_traced(difference, weights)
    return shares


def allocate_difference_traced(
    difference: int, weights: dict[str, int]
) -> tuple[dict[str, int], dict[str, AllocationTraceEntry]]:
    """Same allocation as :func:`allocate_difference`, plus the audit trail.

    Returns ``(shares, trace)`` where ``trace`` maps each key to the
    :class:`AllocationTraceEntry` recorded while computing its share, so the
    trace can never diverge from the allocation it explains.
    """
    if difference == 0:
        shares = {interval: 0 for interval in weights}
        trace = {
            interval: AllocationTraceEntry(
                key=interval,
                weight=weight,
                truncated_share=0,
                remainder=0,
                leftover_units=0,
                share=0,
            )
            for interval, weight in weights.items()
        }
        return shares, trace

    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ZeroTotalWeightError(
            "total absolute branch energy is zero; "
            "cannot allocate a non-zero difference"
        )

    sign = 1 if difference > 0 else -1
    truncated: dict[str, int] = {}
    remainders: dict[str, int] = {}
    truncated_sum = 0
    for interval, weight in weights.items():
        magnitude = abs(difference) * weight
        quotient, remainder = divmod(magnitude, total_weight)
        truncated[interval] = sign * quotient
        remainders[interval] = remainder
        truncated_sum += sign * quotient

    shares = dict(truncated)
    leftover = abs(difference - truncated_sum)
    order = sorted(weights, key=lambda interval: (-remainders[interval], interval))
    leftover_units = {interval: 0 for interval in weights}
    for interval in order[:leftover]:
        shares[interval] += sign
        leftover_units[interval] = sign

    trace = {
        interval: AllocationTraceEntry(
            key=interval,
            weight=weights[interval],
            truncated_share=truncated[interval],
            remainder=remainders[interval],
            leftover_units=leftover_units[interval],
            share=shares[interval],
        )
        for interval in weights
    }
    return shares, trace


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
    shares, _ = allocate_to_branches_traced(allocated, branch_energies)
    return shares


def allocate_to_branches_traced(
    allocated: int, branch_energies: dict[str, int]
) -> tuple[dict[str, int], dict[str, AllocationTraceEntry]]:
    """Same second-level allocation as :func:`allocate_to_branches`, plus the
    per-branch calculation records behind it."""
    weights = {branch: abs(energy) for branch, energy in branch_energies.items()}
    return allocate_difference_traced(allocated, weights)
