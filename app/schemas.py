"""Request/response schemas with fixed-point (0.001 kWh) precision guards."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
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


NonBlankStr = Annotated[str, AfterValidator(_require_non_blank)]


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

    branch: NonBlankStr = Field(min_length=1)
    interval: NonBlankStr = Field(min_length=1)
    energy: MilliDecimal


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


class BranchAllocation(BaseModel):
    """One branch's bookable adjustment inside an interval, in kWh."""

    branch: str
    energy: str
    adjustment: str
    adjusted_energy: str


class IntervalAllocation(BaseModel):
    interval: str
    branch_total: str
    allocated: str
    # Only populated for detail_level="branch"; excluded from the response
    # when None so legacy callers see the exact original fields.
    branch_allocations: list[BranchAllocation] | None = None


class SettlementResponse(BaseModel):
    meter_increment: str
    branch_total: str
    difference: str
    check_sum: str
    allocations: list[IntervalAllocation]
