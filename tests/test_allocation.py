"""Unit tests for the fixed-point largest-remainder allocator.

All expectations are exact integers (milliunits of 0.001 kWh); no float
approximation is used anywhere.
"""

import pytest

from app.allocation import (
    ZeroTotalWeightError,
    allocate_difference,
    format_milliunits,
    summarize_intervals,
    to_milliunits,
)
from decimal import Decimal


# --- interval weights ---------------------------------------------------------

def test_interval_weight_is_sum_of_absolute_branch_energies():
    totals, weights = summarize_intervals(
        [("I1", 4000), ("I1", -4000), ("I2", 2000), ("I2", -500)]
    )
    assert totals == {"I1": 0, "I2": 1500}
    # +/- branches cancel in the total but still contribute their magnitude
    assert weights == {"I1": 8000, "I2": 2500}


def test_cancelled_interval_still_receives_its_share():
    # I1 nets to zero yet holds 80% of the absolute branch energy.
    _, weights = summarize_intervals([("I1", 4000), ("I1", -4000), ("I2", 2000)])
    result = allocate_difference(1000, weights)
    assert result == {"I1": 800, "I2": 200}
    assert sum(result.values()) == 1000


# --- allocation sum -------------------------------------------------------

def test_allocation_sum_equals_difference_exactly():
    weights = {"I1": 4000, "I2": 3500, "I3": 500}  # total weight 8000
    result = allocate_difference(2000, weights)
    assert result == {"I1": 1000, "I2": 875, "I3": 125}
    assert sum(result.values()) == 2000


def test_allocation_sum_with_leftover_milliunits():
    weights = {"I1": 5, "I2": 3, "I3": 2}  # total weight 10
    result = allocate_difference(7, weights)
    # truncated shares 3/2/1 leave 1 unit; I1 has the largest remainder (5)
    assert result == {"I1": 4, "I2": 2, "I3": 1}
    assert sum(result.values()) == 7


@pytest.mark.parametrize(
    "difference", [1, 2, 3, 17, 999, 123456, -1, -2, -17, -999, -123456]
)
def test_sum_invariant_holds_for_any_difference(difference):
    weights = {"a": 7, "b": 13, "c": 1, "d": 0, "e": 29}
    result = allocate_difference(difference, weights)
    assert sum(result.values()) == difference


# --- tie order ------------------------------------------------------------

def test_equal_remainders_fall_back_to_interval_id_ascending():
    # Insertion order is scrambled on purpose; ties must still resolve by id.
    weights = {"I3": 1000, "I1": 1000, "I2": 1000}
    result = allocate_difference(2, weights)
    assert result == {"I1": 1, "I2": 1, "I3": 0}
    assert sum(result.values()) == 2


def test_tie_break_is_lexicographic_not_numeric():
    weights = {"I2": 10, "I10": 10, "I1": 10}  # lexicographic: I1 < I10 < I2
    result = allocate_difference(2, weights)
    assert result == {"I1": 1, "I10": 1, "I2": 0}


def test_tie_break_does_not_depend_on_insertion_order():
    weights = {"c": 4, "a": 4, "b": 4}
    result = allocate_difference(1, weights)
    assert result == {"a": 1, "b": 0, "c": 0}


# --- negative difference boundary -----------------------------------------

def test_negative_share_truncates_toward_zero_not_floor():
    # share = -1/2 milliunit per interval: toward-zero truncation gives 0,
    # floor division would give -1 and corrupt the remainder accounting.
    result = allocate_difference(-1, {"I1": 1, "I2": 1})
    assert result == {"I1": -1, "I2": 0}
    assert sum(result.values()) == -1


def test_negative_difference_hands_out_negative_units_in_same_order():
    weights = {"I1": 1000, "I2": 1000, "I3": 1000}
    result = allocate_difference(-2, weights)
    assert result == {"I1": -1, "I2": -1, "I3": 0}
    assert sum(result.values()) == -2


def test_negative_difference_mirrors_positive_case():
    weights = {"I1": 5, "I2": 3, "I3": 2}
    assert allocate_difference(-7, weights) == {"I1": -4, "I2": -2, "I3": -1}


# --- boundary conditions ----------------------------------------------------

def test_zero_difference_allocates_nothing():
    assert allocate_difference(0, {"I1": 5, "I2": 0}) == {"I1": 0, "I2": 0}


def test_zero_weight_interval_receives_nothing():
    result = allocate_difference(1, {"I1": 0, "I2": 1, "I3": 1})
    assert result == {"I1": 0, "I2": 1, "I3": 0}


def test_zero_total_weight_with_nonzero_difference_is_rejected():
    with pytest.raises(ZeroTotalWeightError):
        allocate_difference(1, {"I1": 0, "I2": 0})
    with pytest.raises(ZeroTotalWeightError):
        allocate_difference(-1, {"I1": 0})


def test_zero_total_weight_with_zero_difference_is_allowed():
    assert allocate_difference(0, {"I1": 0, "I2": 0}) == {"I1": 0, "I2": 0}


# --- fixed-point helpers ----------------------------------------------------

def test_milliunit_roundtrip():
    assert to_milliunits(Decimal("10.005")) == 10005
    assert to_milliunits(Decimal("-0.001")) == -1
    assert to_milliunits(Decimal("0.000")) == 0
    assert format_milliunits(10005) == "10.005"
    assert format_milliunits(-1) == "-0.001"
    assert format_milliunits(0) == "0.000"
    assert format_milliunits(-123456) == "-123.456"
