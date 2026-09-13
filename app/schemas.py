"""Request/response schemas with fixed-point (0.001 kWh) precision guards."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)

MAX_DECIMAL_PLACES = 3


def _coerce_float_via_str(value: Any) -> Any:
    """Convert JSON numbers to Decimal through their shortest string form.

    This keeps a JSON number such as ``10.005`` exact instead of exposing the
    underlying binary floating-point representation.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _check_precision(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("value must be a finite number")
    if value.as_tuple().exponent < -MAX_DECIMAL_PLACES:
        raise ValueError(
            "precision out of bounds: more than 3 decimal places "
            "(smallest unit is 0.001 kWh)"
        )
    return value


MilliDecimal = Annotated[
    Decimal,
    BeforeValidator(_coerce_float_via_str),
    AfterValidator(_check_precision),
]


def _require_non_blank(value: str) -> str:
    """Reject identifiers that consist only of whitespace.

    ``min_length=1`` already rejects the empty string, but ``"   "`` would
    otherwise flow into the allocation and produce unidentifiable bookable
    rows.
    """
    if not value.strip():
        raise ValueError("identifier must not be blank or all whitespace")
    return value


def _require_no_surrounding_whitespace(value: str) -> str:
    """Reject identifiers carrying leading or trailing whitespace.

    ``" B1"`` and ``"B1"`` look identical to a settlement clerk but would be
    booked as two distinct branches (or intervals): detail rows split, the
    leftover 0.001 kWh units land on the wrong id, and a padded
    fixed-adjustment id is misreported as an unknown reading. The format
    anomaly must be rejected up front, never silently normalized.
    """
    if value != value.strip():
        raise ValueError(
            "identifier must not have leading or trailing whitespace"
        )
    return value


IdentifierStr = Annotated[
    str,
    AfterValidator(_require_non_blank),
    AfterValidator(_require_no_surrounding_whitespace),
]


class MeterReadings(BaseModel):
    """Total-meter start/end readings in kWh."""

    model_config = ConfigDict(extra="forbid")

    start: MilliDecimal
    end: MilliDecimal

    @model_validator(mode="after")
    def end_must_not_precede_start(self) -> "MeterReadings":
        if self.end < self.start:
            raise ValueError(
                "meter reading goes backwards: end is smaller than start"
            )
        return self


class BranchReading(BaseModel):
    """One branch's aggregated energy for one interval, in kWh."""

    # A misspelled key (e.g. "enery") must be rejected, not silently dropped.
    model_config = ConfigDict(extra="forbid")

    branch: IdentifierStr = Field(min_length=1)
    interval: IdentifierStr = Field(min_length=1)
    energy: MilliDecimal


class FixedAdjustment(BaseModel):
    """A settlement clerk's confirmed adjustment for one existing reading."""

    model_config = ConfigDict(extra="forbid")

    branch: IdentifierStr = Field(min_length=1)
    interval: IdentifierStr = Field(min_length=1)
    adjustment: MilliDecimal


class SettlementRequest(BaseModel):
    # Unknown top-level fields (e.g. a misspelled "detail_level") must be
    # rejected instead of silently falling back to interval-level results.
    model_config = ConfigDict(extra="forbid")

    meter: MeterReadings
    readings: list[BranchReading] = Field(min_length=1)
    # "interval" (default) keeps the legacy response shape; "branch" adds a
    # per-branch breakdown to every interval. Anything else is a 422 whose
    # loc points at this field.
    detail_level: Literal["interval", "branch"] = "interval"
    # When omitted, responses remain byte-for-byte compatible with the legacy
    # API. An explicit list requires detail_level="branch" and marks each
    # branch adjustment as "fixed" or "calculated".
    fixed_adjustments: list[FixedAdjustment] = Field(default_factory=list)
    # Opt-in audit trail: requires detail_level="branch" and appends
    # calculation_trace to the response. Omitted/false keeps the response
    # field-for-field identical to the untraced shape. Strictly boolean: a
    # numeric switch (e.g. 1/0) must not silently change the response
    # structure, so it is rejected with a 422 located at this field.
    include_trace: StrictBool = False


class BranchAllocation(BaseModel):
    """One branch's bookable adjustment inside an interval, in kWh."""

    branch: str
    energy: str
    adjustment: str
    adjusted_energy: str
    # Present only when fixed_adjustments was explicitly supplied; omitted for
    # legacy requests so their branch-allocation objects keep their old shape.
    source: Literal["fixed", "calculated"] | None = None


class IntervalAllocation(BaseModel):
    interval: str
    branch_total: str
    allocated: str
    # Only populated for detail_level="branch"; excluded from the response
    # when None so legacy callers see the exact original fields.
    branch_allocations: list[BranchAllocation] | None = None


class BranchCalculationTrace(BaseModel):
    """One unlocked branch's second-level calculation record.

    ``weight``/``truncated_share``/``adjustment`` are kWh strings; ``remainder``
    is the raw fixed-point remainder used to rank leftover hand-outs and
    ``leftover_units`` the signed count of 0.001 kWh units this branch received
    in that pass.
    """

    branch: str
    weight: str
    truncated_share: str
    remainder: int
    leftover_units: int
    adjustment: str


class IntervalCalculationTrace(BaseModel):
    """One interval's first-level calculation record plus its branch records.

    With fixed_adjustments, ``weight``/``truncated_share``/``remainder``/
    ``leftover_units``/``residual_allocated`` describe only the residual
    difference split among unlocked readings, while ``fixed_total`` keeps the
    locked adjustments' contribution and ``allocated`` is the interval's final
    result (``residual_allocated + fixed_total``).
    """

    interval: str
    weight: str
    truncated_share: str
    remainder: int
    leftover_units: int
    residual_allocated: str
    fixed_total: str
    allocated: str
    branches: list[BranchCalculationTrace]


class CalculationTrace(BaseModel):
    """Audit trail of the fixed-point largest-remainder computation.

    ``difference`` is the original meter difference, ``fixed_total`` the sum of
    locked adjustments and ``residual_difference`` the remainder that was
    actually allocated (``difference - fixed_total``). ``intervals`` follows
    the same ordering as the response's ``allocations``.
    """

    difference: str
    fixed_total: str
    residual_difference: str
    intervals: list[IntervalCalculationTrace]


class SettlementResponse(BaseModel):
    meter_increment: str
    branch_total: str
    difference: str
    check_sum: str
    allocations: list[IntervalAllocation]
    # Present only when the request asked for include_trace=true; omitted
    # otherwise so untraced responses keep their exact original fields.
    calculation_trace: CalculationTrace | None = None


# --- meter reading sequence derivation --------------------------------------


def _require_positive_range(value: Decimal) -> Decimal:
    """The full-scale upper bound must be a positive three-decimal value.

    A zero or negative range makes the rollover formula
    (``range_max - start + end``) meaningless, so it must be rejected up
    front with the error located at the ``range_max`` field.
    """
    if value <= 0:
        raise ValueError(
            "range_max must be greater than zero (smallest positive range "
            "is 0.001 kWh)"
        )
    return value


RangeMaxDecimal = Annotated[
    MilliDecimal,
    AfterValidator(_require_positive_range),
]


class MeterSample(BaseModel):
    """One time-stamped cumulative meter indication, in kWh."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    value: MilliDecimal


class MeterSequenceRequest(BaseModel):
    """A meter id, its three-decimal full-scale range and the time-ordered
    cumulative samples, with one segment declaration per adjacent pair."""

    model_config = ConfigDict(extra="forbid")

    meter_id: IdentifierStr = Field(min_length=1)
    range_max: RangeMaxDecimal
    # At least two samples are needed to form one interval; the
    # len(segments) == len(samples) - 1 relationship is checked in the
    # service so the error can point precisely at "segments".
    samples: list[MeterSample] = Field(min_length=2)
    segments: list[Literal["normal", "rollover"]] = Field(min_length=1)


class MeterInterval(BaseModel):
    """One derived interval energy in kWh, sorted by start time."""

    index: int
    start_time: datetime
    end_time: datetime
    start_value: str
    end_value: str
    segment_type: Literal["normal", "rollover"]
    energy: str


class MeterSequenceResponse(BaseModel):
    meter_id: str
    range_max: str
    intervals: list[MeterInterval]
    total_energy: str
