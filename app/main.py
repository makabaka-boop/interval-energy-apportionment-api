"""FastAPI application exposing the meter-difference allocation endpoint."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .allocation import (
    ZeroTotalWeightError,
    allocate_difference,
    format_milliunits,
    to_milliunits,
)
from .schemas import (
    IntervalAllocation,
    SettlementRequest,
    SettlementResponse,
)

app = FastAPI(
    title="Park Meter Difference Allocation API",
    version="1.0.0",
    description=(
        "Allocates the difference between the total meter increment and the "
        "sum of branch energies across intervals with a fixed-point largest "
        "remainder method (0.001 kWh resolution)."
    ),
)


class ApiError(Exception):
    """Business-rule rejection rendered in the same envelope as 422 validation errors."""

    def __init__(self, status_code: int, loc: list, msg: str, err_type: str) -> None:
        super().__init__(msg)
        self.status_code = status_code
        self.loc = loc
        self.msg = msg
        self.err_type = err_type


def _detail(loc: list, msg: str, err_type: str) -> dict:
    return {"loc": loc, "msg": msg, "type": err_type}


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": [_detail(exc.loc, exc.msg, exc.err_type)]},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        _detail(list(error["loc"]), error["msg"], error["type"])
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/settlements/allocate", response_model=SettlementResponse)
def allocate_settlement(payload: SettlementRequest) -> SettlementResponse:
    # Reject duplicate (branch, interval) readings, pointing at the offending item.
    seen: dict[tuple[str, str], int] = {}
    for index, reading in enumerate(payload.readings):
        key = (reading.branch, reading.interval)
        if key in seen:
            raise ApiError(
                422,
                ["body", "readings", index],
                f"duplicate reading for branch '{reading.branch}' in interval "
                f"'{reading.interval}' (first occurrence at index {seen[key]})",
                "duplicate_reading",
            )
        seen[key] = index

    # Every branch must report exactly the same set of intervals.
    intervals_by_branch: dict[str, set[str]] = {}
    for reading in payload.readings:
        intervals_by_branch.setdefault(reading.branch, set()).add(reading.interval)
    reference_branch = min(intervals_by_branch)
    reference_intervals = intervals_by_branch[reference_branch]
    for branch in sorted(intervals_by_branch):
        branch_intervals = intervals_by_branch[branch]
        if branch_intervals != reference_intervals:
            missing = sorted(reference_intervals - branch_intervals)
            extra = sorted(branch_intervals - reference_intervals)
            raise ApiError(
                422,
                ["body", "readings"],
                f"interval set mismatch: branch '{branch}' differs from branch "
                f"'{reference_branch}' (missing intervals: {missing}, "
                f"extra intervals: {extra})",
                "interval_set_mismatch",
            )

    # Aggregate branch energies per interval in fixed point.
    interval_totals: dict[str, int] = {}
    for reading in payload.readings:
        interval_totals[reading.interval] = (
            interval_totals.get(reading.interval, 0)
            + to_milliunits(reading.energy)
        )

    meter_increment = to_milliunits(payload.meter.end) - to_milliunits(payload.meter.start)
    branch_total = sum(interval_totals.values())
    difference = meter_increment - branch_total
    weights = {interval: abs(total) for interval, total in interval_totals.items()}

    try:
        allocation = allocate_difference(difference, weights)
    except ZeroTotalWeightError as exc:
        raise ApiError(422, ["body", "readings"], str(exc), "zero_total_weight") from exc

    check_sum = sum(allocation.values())
    if check_sum != difference:  # internal invariant, can never trigger
        raise AssertionError("allocation does not sum to the difference")

    allocations = [
        IntervalAllocation(
            interval=interval,
            branch_total=format_milliunits(interval_totals[interval]),
            allocated=format_milliunits(allocation[interval]),
        )
        for interval in sorted(interval_totals)
    ]
    return SettlementResponse(
        meter_increment=format_milliunits(meter_increment),
        branch_total=format_milliunits(branch_total),
        difference=format_milliunits(difference),
        check_sum=format_milliunits(check_sum),
        allocations=allocations,
    )
