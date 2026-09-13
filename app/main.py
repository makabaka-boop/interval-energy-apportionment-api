"""FastAPI application exposing the meter-difference allocation endpoint."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .allocation import (
    ZeroTotalWeightError,
    allocate_difference_traced,
    allocate_to_branches_traced,
    format_milliunits,
    summarize_intervals,
    to_milliunits,
)
from .meter_sequence import SequenceError, derive_sequence
from .schemas import (
    BranchAllocation,
    BranchCalculationTrace,
    CalculationTrace,
    IntervalAllocation,
    IntervalCalculationTrace,
    MeterInterval,
    MeterSequenceRequest,
    MeterSequenceResponse,
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


@app.post(
    "/api/v1/settlements/allocate",
    response_model=SettlementResponse,
    # Requests without detail_level="branch" must keep the exact legacy
    # response shape, so the optional branch_allocations field is omitted
    # entirely when unset.
    response_model_exclude_none=True,
)
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

    # The audit trail breaks every interval down to its branches, so it only
    # exists alongside the per-branch detail view.
    if payload.include_trace and payload.detail_level != "branch":
        raise ApiError(
            422,
            ["body", "include_trace"],
            "include_trace requires detail_level='branch'",
            "include_trace_requires_branch_detail",
        )

    # fixed_adjustments is opt-in: merely omitting it preserves the legacy
    # request and response shape, while an explicit empty list still asks for
    # every branch adjustment to be labelled "calculated".
    has_fixed_adjustments = "fixed_adjustments" in payload.model_fields_set
    fixed_adjustments: dict[tuple[str, str], int] = {}
    if has_fixed_adjustments:
        if payload.detail_level != "branch":
            raise ApiError(
                422,
                ["body", "fixed_adjustments"],
                "fixed_adjustments requires detail_level='branch'",
                "fixed_adjustments_requires_branch_detail",
            )

        fixed_seen: dict[tuple[str, str], int] = {}
        for index, item in enumerate(payload.fixed_adjustments):
            key = (item.branch, item.interval)
            if key in fixed_seen:
                raise ApiError(
                    422,
                    ["body", "fixed_adjustments", index],
                    f"duplicate fixed adjustment for branch '{item.branch}' in "
                    f"interval '{item.interval}' (first occurrence at index "
                    f"{fixed_seen[key]})",
                    "duplicate_fixed_adjustment",
                )
            if key not in seen:
                raise ApiError(
                    422,
                    ["body", "fixed_adjustments", index],
                    f"fixed adjustment must reference a reading in this request: "
                    f"branch '{item.branch}', interval '{item.interval}'",
                    "unknown_fixed_adjustment",
                )
            fixed_seen[key] = index
            fixed_adjustments[key] = to_milliunits(item.adjustment)

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

    # Convert once; the same milliunit values are used for totals, weights,
    # fixed adjustments, and branch-level adjusted energies.
    energies_by_interval: dict[str, dict[str, int]] = {}
    reading_energies: list[int] = []
    for reading in payload.readings:
        energy = to_milliunits(reading.energy)
        reading_energies.append(energy)
        energies_by_interval.setdefault(reading.interval, {})[reading.branch] = energy

    # Aggregate per interval: signed branch total, and weight = sum of the
    # absolute branch energies (cancelling +/- branches still weigh in).
    interval_totals, weights = summarize_intervals(
        (reading.interval, energy)
        for reading, energy in zip(payload.readings, reading_energies)
    )

    meter_increment = to_milliunits(payload.meter.end) - to_milliunits(payload.meter.start)
    branch_total = sum(interval_totals.values())
    difference = meter_increment - branch_total

    fixed_by_interval: dict[str, int] = {}
    fixed_total = 0
    if has_fixed_adjustments:
        for index, item in enumerate(payload.fixed_adjustments):
            amount = fixed_adjustments[(item.branch, item.interval)]
            if amount != 0 and (
                difference == 0 or (amount > 0) != (difference > 0)
            ):
                raise ApiError(
                    422,
                    ["body", "fixed_adjustments", index, "adjustment"],
                    f"fixed adjustment {format_milliunits(amount)} has the "
                    f"opposite sign from the meter difference "
                    f"{format_milliunits(difference)}",
                    "fixed_adjustment_sign_mismatch",
                )

        absolute_total = 0
        for index, item in enumerate(payload.fixed_adjustments):
            amount = fixed_adjustments[(item.branch, item.interval)]
            absolute_total += abs(amount)
            if absolute_total > abs(difference):
                raise ApiError(
                    422,
                    ["body", "fixed_adjustments", index, "adjustment"],
                    f"absolute fixed adjustments total "
                    f"{format_milliunits(absolute_total)} exceeds meter "
                    f"difference magnitude {format_milliunits(abs(difference))}",
                    "fixed_adjustment_total_exceeds_difference",
                )
            fixed_by_interval[item.interval] = (
                fixed_by_interval.get(item.interval, 0) + amount
            )
            fixed_total += amount

        unlocked_weights: dict[str, int] = {interval: 0 for interval in weights}
        first_unlocked_reading: int | None = None
        for index, reading in enumerate(payload.readings):
            key = (reading.branch, reading.interval)
            if key in fixed_adjustments:
                continue
            if first_unlocked_reading is None:
                first_unlocked_reading = index
            unlocked_weights[reading.interval] += abs(reading_energies[index])

        residual_difference = difference - fixed_total
        if residual_difference != 0 and sum(unlocked_weights.values()) <= 0:
            if first_unlocked_reading is not None:
                loc = ["body", "readings", first_unlocked_reading]
            else:
                loc = ["body", "fixed_adjustments", 0]
            raise ApiError(
                422,
                loc,
                "a non-zero residual remains after fixed adjustments but every "
                "unlocked reading has zero absolute energy",
                "zero_unlocked_weight",
            )
    else:
        unlocked_weights = weights
        residual_difference = difference

    try:
        residual_allocation, interval_calc = allocate_difference_traced(
            residual_difference, unlocked_weights
        )
    except ZeroTotalWeightError as exc:
        error_type = (
            "zero_unlocked_weight" if has_fixed_adjustments else "zero_total_weight"
        )
        raise ApiError(422, ["body", "readings"], str(exc), error_type) from exc

    if fixed_adjustments:
        allocation = {
            interval: residual_allocation[interval] + fixed_by_interval.get(interval, 0)
            for interval in interval_totals
        }
    else:
        allocation = residual_allocation

    check_sum = sum(allocation.values())
    if check_sum != difference:  # internal invariant, can never trigger
        raise AssertionError("allocation does not sum to the difference")

    # Second level only when requested: per interval, spread its allocated
    # share across branches proportionally to absolute branch energy.
    show_branch_allocations = (
        payload.detail_level == "branch" or has_fixed_adjustments
    )

    allocations = []
    traced_intervals: list[IntervalCalculationTrace] = []
    for interval in sorted(interval_totals):
        branch_allocations = None
        branch_traces: list[BranchCalculationTrace] = []
        if show_branch_allocations:
            energies = energies_by_interval[interval]
            if fixed_adjustments:
                unlocked_energies = {
                    branch: energy
                    for branch, energy in energies.items()
                    if (branch, interval) not in fixed_adjustments
                }
                calculated_adjustments, branch_calc = allocate_to_branches_traced(
                    residual_allocation[interval], unlocked_energies
                )
                adjustments = {
                    branch: (
                        fixed_adjustments[(branch, interval)]
                        if (branch, interval) in fixed_adjustments
                        else calculated_adjustments[branch]
                    )
                    for branch in energies
                }
            else:
                adjustments, branch_calc = allocate_to_branches_traced(
                    allocation[interval], energies
                )

            if payload.include_trace:
                # Only calculated (unlocked) branches appear here; locked ones
                # are not residual-calculation items and stay visible through
                # the interval's fixed_total below.
                branch_traces = [
                    BranchCalculationTrace(
                        branch=branch,
                        weight=format_milliunits(branch_calc[branch].weight),
                        truncated_share=format_milliunits(
                            branch_calc[branch].truncated_share
                        ),
                        remainder=branch_calc[branch].remainder,
                        leftover_units=branch_calc[branch].leftover_units,
                        adjustment=format_milliunits(branch_calc[branch].share),
                    )
                    for branch in sorted(branch_calc)
                ]

            branch_allocations = [
                BranchAllocation(
                    branch=branch,
                    energy=format_milliunits(energies[branch]),
                    adjustment=format_milliunits(adjustments[branch]),
                    adjusted_energy=format_milliunits(
                        energies[branch] + adjustments[branch]
                    ),
                    source=(
                        "fixed"
                        if (branch, interval) in fixed_adjustments
                        else "calculated"
                    )
                    if has_fixed_adjustments
                    else None,
                )
                for branch in sorted(energies)
            ]
        allocations.append(
            IntervalAllocation(
                interval=interval,
                branch_total=format_milliunits(interval_totals[interval]),
                allocated=format_milliunits(allocation[interval]),
                branch_allocations=branch_allocations,
            )
        )
        if payload.include_trace:
            entry = interval_calc[interval]
            traced_intervals.append(
                IntervalCalculationTrace(
                    interval=interval,
                    weight=format_milliunits(entry.weight),
                    truncated_share=format_milliunits(entry.truncated_share),
                    remainder=entry.remainder,
                    leftover_units=entry.leftover_units,
                    residual_allocated=format_milliunits(
                        residual_allocation[interval]
                    ),
                    fixed_total=format_milliunits(
                        fixed_by_interval.get(interval, 0)
                    ),
                    allocated=format_milliunits(allocation[interval]),
                    branches=branch_traces,
                )
            )
    calculation_trace = None
    if payload.include_trace:
        calculation_trace = CalculationTrace(
            difference=format_milliunits(difference),
            fixed_total=format_milliunits(fixed_total),
            residual_difference=format_milliunits(residual_difference),
            intervals=traced_intervals,
        )
    return SettlementResponse(
        meter_increment=format_milliunits(meter_increment),
        branch_total=format_milliunits(branch_total),
        difference=format_milliunits(difference),
        check_sum=format_milliunits(check_sum),
        allocations=allocations,
        calculation_trace=calculation_trace,
    )


@app.post(
    "/api/v1/meter-sequences/derive",
    response_model=MeterSequenceResponse,
)
def derive_meter_sequence(payload: MeterSequenceRequest) -> MeterSequenceResponse:
    # Independently of the allocation endpoint: convert once to milliunit
    # integers, derive each declared interval, and let the service reject
    # the whole request (never a partial interval) on ordering, range or
    # direction contradictions.
    range_max = to_milliunits(payload.range_max)
    values = [to_milliunits(sample.value) for sample in payload.samples]
    timestamps = [sample.timestamp for sample in payload.samples]
    try:
        derivation = derive_sequence(
            range_max, values, timestamps, payload.segments
        )
    except SequenceError as exc:
        # Service locs are body-relative; the wire loc starts with "body".
        raise ApiError(422, ["body", *exc.loc], exc.msg, exc.err_type) from exc

    intervals = [
        MeterInterval(
            index=segment.index,
            start_time=segment.start_time,
            end_time=segment.end_time,
            start_value=format_milliunits(segment.start_value),
            end_value=format_milliunits(segment.end_value),
            segment_type=segment.segment_type,
            energy=format_milliunits(segment.energy),
        )
        for segment in derivation.segments
    ]
    return MeterSequenceResponse(
        meter_id=payload.meter_id,
        range_max=format_milliunits(derivation.range_max),
        intervals=intervals,
        total_energy=format_milliunits(derivation.total_energy),
    )
