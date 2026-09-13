"""One-shot acceptance service.

Waits for the API to become healthy, then replays a fixed set of settlement
scenarios and checks every digit of the responses (exact strings, no float
approximation). Exits 0 when all checks pass, 1 otherwise.

Configure the target with API_BASE_URL (default http://localhost:8000).
"""

from __future__ import annotations

import os
import sys
import time

import httpx

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
ALLOCATE_URL = f"{BASE_URL}/api/v1/settlements/allocate"
HEALTH_URL = f"{BASE_URL}/healthz"

FAILURES: list[str] = []


def check(name: str, condition: bool, context: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        FAILURES.append(name)
        if context:
            print(f"       {context}")


def wait_for_api(timeout_seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(HEALTH_URL, timeout=2.0)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def post(payload: dict) -> httpx.Response:
    return httpx.post(ALLOCATE_URL, json=payload, timeout=10.0)


def main() -> int:
    if not wait_for_api():
        print(f"[FAIL] API at {BASE_URL} did not become healthy in time")
        return 1
    print(f"API at {BASE_URL} is healthy, running acceptance checks...")

    # 1. Exact allocation: difference 2.000 kWh over weights 4000/3500/500.
    payload = {
        "meter": {"start": "100.000", "end": "110.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "3.000"},
            {"branch": "B2", "interval": "I1", "energy": "1.000"},
            {"branch": "B1", "interval": "I2", "energy": "2.000"},
            {"branch": "B2", "interval": "I2", "energy": "1.500"},
            {"branch": "B1", "interval": "I3", "energy": "0.500"},
            {"branch": "B2", "interval": "I3", "energy": "0.000"},
        ],
    }
    response = post(payload)
    check("basic allocation returns 200", response.status_code == 200, response.text)
    if response.status_code == 200:
        check(
            "basic allocation body is exact",
            response.json()
            == {
                "meter_increment": "10.000",
                "branch_total": "8.000",
                "difference": "2.000",
                "check_sum": "2.000",
                "allocations": [
                    {"interval": "I1", "branch_total": "4.000", "allocated": "1.000"},
                    {"interval": "I2", "branch_total": "3.500", "allocated": "0.875"},
                    {"interval": "I3", "branch_total": "0.500", "allocated": "0.125"},
                ],
            },
            response.text,
        )

    # 2. Tied remainders resolve by ascending interval id; response sorted.
    tie_payload = {
        "meter": {"start": "0.000", "end": "3.002"},
        "readings": [
            {"branch": "B1", "interval": "I3", "energy": "1.000"},
            {"branch": "B1", "interval": "I1", "energy": "1.000"},
            {"branch": "B1", "interval": "I2", "energy": "1.000"},
        ],
    }
    response = post(tie_payload)
    check("tie-order allocation returns 200", response.status_code == 200, response.text)
    if response.status_code == 200:
        body = response.json()
        check(
            "tied remainders go to lexicographically first intervals",
            body["allocations"]
            == [
                {"interval": "I1", "branch_total": "1.000", "allocated": "0.001"},
                {"interval": "I2", "branch_total": "1.000", "allocated": "0.001"},
                {"interval": "I3", "branch_total": "1.000", "allocated": "0.000"},
            ]
            and body["check_sum"] == "0.002",
            response.text,
        )

    # 3. Negative difference hands out negative units in the same order.
    negative_payload = {
        "meter": {"start": "10.000", "end": "12.998"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "1.000"},
            {"branch": "B1", "interval": "I2", "energy": "1.000"},
            {"branch": "B1", "interval": "I3", "energy": "1.000"},
        ],
    }
    response = post(negative_payload)
    check("negative difference returns 200", response.status_code == 200, response.text)
    if response.status_code == 200:
        body = response.json()
        check(
            "negative difference allocates -0.001/-0.001/0.000 and balances",
            body["difference"] == "-0.002"
            and body["check_sum"] == "-0.002"
            and body["allocations"]
            == [
                {"interval": "I1", "branch_total": "1.000", "allocated": "-0.001"},
                {"interval": "I2", "branch_total": "1.000", "allocated": "-0.001"},
                {"interval": "I3", "branch_total": "1.000", "allocated": "0.000"},
            ],
            response.text,
        )

    # 4. Cancelling +/- branches: weight is the sum of absolute branch
    #    energies, so the cancelled interval still receives its share.
    cancel_payload = {
        "meter": {"start": "0.000", "end": "3.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "4.000"},
            {"branch": "B2", "interval": "I1", "energy": "-4.000"},
            {"branch": "B1", "interval": "I2", "energy": "2.000"},
            {"branch": "B2", "interval": "I2", "energy": "0.000"},
        ],
    }
    response = post(cancel_payload)
    check("cancelling branches return 200", response.status_code == 200, response.text)
    if response.status_code == 200:
        body = response.json()
        check(
            "cancelled interval keeps weight from absolute branch energies",
            body["difference"] == "1.000"
            and body["check_sum"] == "1.000"
            and body["allocations"]
            == [
                {"interval": "I1", "branch_total": "0.000", "allocated": "0.800"},
                {"interval": "I2", "branch_total": "2.000", "allocated": "0.200"},
            ],
            response.text,
        )

    # 5. Determinism: identical requests produce byte-identical responses.
    first, second = post(payload), post(payload)
    check(
        "identical requests are byte-identical",
        first.status_code == 200 and first.content == second.content,
    )

    # 6. Branch detail: every interval's allocated amount is conserved by the
    #    second-level per-branch split (exact full-body comparison).
    response = post({**payload, "detail_level": "branch"})
    check("branch detail returns 200", response.status_code == 200, response.text)
    if response.status_code == 200:
        check(
            "branch allocations conserve each interval's allocated amount",
            response.json()
            == {
                "meter_increment": "10.000",
                "branch_total": "8.000",
                "difference": "2.000",
                "check_sum": "2.000",
                "allocations": [
                    {
                        "interval": "I1",
                        "branch_total": "4.000",
                        "allocated": "1.000",
                        "branch_allocations": [
                            {"branch": "B1", "energy": "3.000",
                             "adjustment": "0.750", "adjusted_energy": "3.750"},
                            {"branch": "B2", "energy": "1.000",
                             "adjustment": "0.250", "adjusted_energy": "1.250"},
                        ],
                    },
                    {
                        "interval": "I2",
                        "branch_total": "3.500",
                        "allocated": "0.875",
                        "branch_allocations": [
                            {"branch": "B1", "energy": "2.000",
                             "adjustment": "0.500", "adjusted_energy": "2.500"},
                            {"branch": "B2", "energy": "1.500",
                             "adjustment": "0.375", "adjusted_energy": "1.875"},
                        ],
                    },
                    {
                        "interval": "I3",
                        "branch_total": "0.500",
                        "allocated": "0.125",
                        "branch_allocations": [
                            {"branch": "B1", "energy": "0.500",
                             "adjustment": "0.125", "adjusted_energy": "0.625"},
                            {"branch": "B2", "energy": "0.000",
                             "adjustment": "0.000", "adjusted_energy": "0.000"},
                        ],
                    },
                ],
            },
            response.text,
        )

    # 7. Negative difference with tied branches: the leftover negative unit
    #    goes to the lexicographically first branch, response sorted by branch.
    tied_branch_payload = {
        "detail_level": "branch",
        "meter": {"start": "1.000", "end": "1.997"},
        "readings": [
            {"branch": "B2", "interval": "I1", "energy": "0.500"},
            {"branch": "B1", "interval": "I1", "energy": "0.500"},
        ],
    }
    response = post(tied_branch_payload)
    check(
        "negative branch detail returns 200",
        response.status_code == 200,
        response.text,
    )
    if response.status_code == 200:
        body = response.json()
        check(
            "tied branches split -0.003 as -0.002/-0.001 in branch-id order",
            body["difference"] == "-0.003"
            and body["check_sum"] == "-0.003"
            and body["allocations"]
            == [
                {
                    "interval": "I1",
                    "branch_total": "1.000",
                    "allocated": "-0.003",
                    "branch_allocations": [
                        {"branch": "B1", "energy": "0.500",
                         "adjustment": "-0.002", "adjusted_energy": "0.498"},
                        {"branch": "B2", "energy": "0.500",
                         "adjustment": "-0.001", "adjusted_energy": "0.499"},
                    ],
                }
            ],
            response.text,
        )

    # 8. A clerk-confirmed branch adjustment is locked out of proportional
    #    allocation; the remaining difference is split among unlocked readings
    #    and every branch row records whether its source is fixed/calculated.
    fixed_payload = {
        **payload,
        "detail_level": "branch",
        "fixed_adjustments": [
            {"branch": "B2", "interval": "I1", "adjustment": "0.500"}
        ],
    }
    response = post(fixed_payload)
    check("fixed adjustment settlement returns 200", response.status_code == 200, response.text)
    if response.status_code == 200:
        check(
            "fixed settlement recomputes exact final branch details",
            response.json()
            == {
                "meter_increment": "10.000",
                "branch_total": "8.000",
                "difference": "2.000",
                "check_sum": "2.000",
                "allocations": [
                    {
                        "interval": "I1",
                        "branch_total": "4.000",
                        "allocated": "1.143",
                        "branch_allocations": [
                            {"branch": "B1", "energy": "3.000",
                             "adjustment": "0.643", "adjusted_energy": "3.643",
                             "source": "calculated"},
                            {"branch": "B2", "energy": "1.000",
                             "adjustment": "0.500", "adjusted_energy": "1.500",
                             "source": "fixed"},
                        ],
                    },
                    {
                        "interval": "I2",
                        "branch_total": "3.500",
                        "allocated": "0.750",
                        "branch_allocations": [
                            {"branch": "B1", "energy": "2.000",
                             "adjustment": "0.429", "adjusted_energy": "2.429",
                             "source": "calculated"},
                            {"branch": "B2", "energy": "1.500",
                             "adjustment": "0.321", "adjusted_energy": "1.821",
                             "source": "calculated"},
                        ],
                    },
                    {
                        "interval": "I3",
                        "branch_total": "0.500",
                        "allocated": "0.107",
                        "branch_allocations": [
                            {"branch": "B1", "energy": "0.500",
                             "adjustment": "0.107", "adjusted_energy": "0.607",
                             "source": "calculated"},
                            {"branch": "B2", "energy": "0.000",
                             "adjustment": "0.000", "adjusted_energy": "0.000",
                             "source": "calculated"},
                        ],
                    },
                ],
            },
            response.text,
        )

    # 9. Invalid detail_level is rejected with loc on that field and no
    #    partial results; omitting it (or "interval") keeps the legacy shape.
    invalid_level = post({**payload, "detail_level": "daily"})
    check(
        "invalid detail_level rejected with loc on detail_level",
        invalid_level.status_code == 422
        and set(invalid_level.json().keys()) == {"detail"}
        and "detail_level" in invalid_level.json()["detail"][0]["loc"],
        invalid_level.text,
    )

    legacy = post(payload)
    explicit_interval = post({**payload, "detail_level": "interval"})
    check(
        "omitted detail_level keeps the exact legacy branch-free response",
        legacy.status_code == 200
        and explicit_interval.content == legacy.content
        and all(
            "branch_allocations" not in item
            for item in legacy.json()["allocations"]
        ),
        legacy.text,
    )

    # 10. Rejections carry field-locatable errors and no partial results.
    backwards = post({"meter": {"start": "5.000", "end": "4.000"},
                      "readings": [{"branch": "B1", "interval": "I1", "energy": "0.000"}]})
    check(
        "backwards meter reading rejected with loc on meter",
        backwards.status_code == 422
        and set(backwards.json().keys()) == {"detail"}
        and "meter" in backwards.json()["detail"][0]["loc"],
        backwards.text,
    )

    too_precise = post({"meter": {"start": "0.000", "end": "1.000"},
                        "readings": [{"branch": "B1", "interval": "I1", "energy": "0.0001"}]})
    detail = too_precise.json().get("detail", [{}])[0] if too_precise.status_code == 422 else {}
    check(
        "precision violation rejected with loc on readings.0.energy",
        too_precise.status_code == 422
        and detail.get("loc") == ["body", "readings", 0, "energy"],
        too_precise.text,
    )

    mismatch = post({
        "meter": {"start": "0.000", "end": "1.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.500"},
            {"branch": "B1", "interval": "I2", "energy": "0.200"},
            {"branch": "B2", "interval": "I1", "energy": "0.100"},
        ],
    })
    check(
        "interval set mismatch rejected",
        mismatch.status_code == 422
        and mismatch.json()["detail"][0]["type"] == "interval_set_mismatch",
        mismatch.text,
    )

    zero_weight = post({
        "meter": {"start": "0.000", "end": "0.001"},
        "readings": [{"branch": "B1", "interval": "I1", "energy": "0.000"}],
    })
    check(
        "zero total weight with non-zero difference rejected",
        zero_weight.status_code == 422
        and zero_weight.json()["detail"][0]["type"] == "zero_total_weight",
        zero_weight.text,
    )

    # 11. Calculation trace: difference 0.007 over interval weights 5/3/2
    #     leaves one milliunit to place; the trace must record every weight,
    #     truncated share, remainder and leftover unit exactly as computed.
    trace_payload = {
        "detail_level": "branch",
        "include_trace": True,
        "meter": {"start": "0.000", "end": "0.017"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.003"},
            {"branch": "B2", "interval": "I1", "energy": "0.002"},
            {"branch": "B1", "interval": "I2", "energy": "0.003"},
            {"branch": "B2", "interval": "I2", "energy": "0.000"},
            {"branch": "B1", "interval": "I3", "energy": "0.002"},
            {"branch": "B2", "interval": "I3", "energy": "0.000"},
        ],
    }
    response = post(trace_payload)
    check("traced settlement returns 200", response.status_code == 200, response.text)
    if response.status_code == 200:
        check(
            "trace records exact positive-difference remainder fill-in",
            response.json()
            == {
                "meter_increment": "0.017",
                "branch_total": "0.010",
                "difference": "0.007",
                "check_sum": "0.007",
                "allocations": [
                    {"interval": "I1", "branch_total": "0.005", "allocated": "0.004",
                     "branch_allocations": [
                         {"branch": "B1", "energy": "0.003", "adjustment": "0.002",
                          "adjusted_energy": "0.005"},
                         {"branch": "B2", "energy": "0.002", "adjustment": "0.002",
                          "adjusted_energy": "0.004"},
                     ]},
                    {"interval": "I2", "branch_total": "0.003", "allocated": "0.002",
                     "branch_allocations": [
                         {"branch": "B1", "energy": "0.003", "adjustment": "0.002",
                          "adjusted_energy": "0.005"},
                         {"branch": "B2", "energy": "0.000", "adjustment": "0.000",
                          "adjusted_energy": "0.000"},
                     ]},
                    {"interval": "I3", "branch_total": "0.002", "allocated": "0.001",
                     "branch_allocations": [
                         {"branch": "B1", "energy": "0.002", "adjustment": "0.001",
                          "adjusted_energy": "0.003"},
                         {"branch": "B2", "energy": "0.000", "adjustment": "0.000",
                          "adjusted_energy": "0.000"},
                     ]},
                ],
                "calculation_trace": {
                    "difference": "0.007",
                    "fixed_total": "0.000",
                    "residual_difference": "0.007",
                    "intervals": [
                        {"interval": "I1", "weight": "0.005",
                         "truncated_share": "0.003", "remainder": 5,
                         "leftover_units": 1, "residual_allocated": "0.004",
                         "fixed_total": "0.000", "allocated": "0.004",
                         "branches": [
                             {"branch": "B1", "weight": "0.003",
                              "truncated_share": "0.002", "remainder": 2,
                              "leftover_units": 0, "adjustment": "0.002"},
                             {"branch": "B2", "weight": "0.002",
                              "truncated_share": "0.001", "remainder": 3,
                              "leftover_units": 1, "adjustment": "0.002"},
                         ]},
                        {"interval": "I2", "weight": "0.003",
                         "truncated_share": "0.002", "remainder": 1,
                         "leftover_units": 0, "residual_allocated": "0.002",
                         "fixed_total": "0.000", "allocated": "0.002",
                         "branches": [
                             {"branch": "B1", "weight": "0.003",
                              "truncated_share": "0.002", "remainder": 0,
                              "leftover_units": 0, "adjustment": "0.002"},
                             {"branch": "B2", "weight": "0.000",
                              "truncated_share": "0.000", "remainder": 0,
                              "leftover_units": 0, "adjustment": "0.000"},
                         ]},
                        {"interval": "I3", "weight": "0.002",
                         "truncated_share": "0.001", "remainder": 4,
                         "leftover_units": 0, "residual_allocated": "0.001",
                         "fixed_total": "0.000", "allocated": "0.001",
                         "branches": [
                             {"branch": "B1", "weight": "0.002",
                              "truncated_share": "0.001", "remainder": 0,
                              "leftover_units": 0, "adjustment": "0.001"},
                             {"branch": "B2", "weight": "0.000",
                              "truncated_share": "0.000", "remainder": 0,
                              "leftover_units": 0, "adjustment": "0.000"},
                         ]},
                    ],
                },
            },
            response.text,
        )

    # 12. Negative difference: the trace records the same remainder ranking
    #     handing out negative milliunits.
    negative_trace_payload = {**trace_payload, "meter": {"start": "0.000", "end": "0.003"}}
    response = post(negative_trace_payload)
    check(
        "traced negative settlement returns 200",
        response.status_code == 200,
        response.text,
    )
    if response.status_code == 200:
        body = response.json()
        check(
            "trace records negative leftover units in the same order",
            body["difference"] == "-0.007"
            and body["check_sum"] == "-0.007"
            and body["calculation_trace"]["intervals"][0]
            == {
                "interval": "I1",
                "weight": "0.005",
                "truncated_share": "-0.003",
                "remainder": 5,
                "leftover_units": -1,
                "residual_allocated": "-0.004",
                "fixed_total": "0.000",
                "allocated": "-0.004",
                "branches": [
                    {"branch": "B1", "weight": "0.003",
                     "truncated_share": "-0.002", "remainder": 2,
                     "leftover_units": 0, "adjustment": "-0.002"},
                    {"branch": "B2", "weight": "0.002",
                     "truncated_share": "-0.001", "remainder": 3,
                     "leftover_units": -1, "adjustment": "-0.002"},
                ],
            },
            response.text,
        )

    # 13. Trace with locked adjustments: only the residual difference is
    #     explained per calculation item, while each interval keeps its locked
    #     contribution (residual_allocated + fixed_total == allocated).
    response = post({**fixed_payload, "include_trace": True})
    check(
        "traced fixed settlement returns 200",
        response.status_code == 200,
        response.text,
    )
    if response.status_code == 200:
        body = response.json()
        check(
            "trace explains residual chain and keeps locked contributions",
            body["calculation_trace"]
            == {
                "difference": "2.000",
                "fixed_total": "0.500",
                "residual_difference": "1.500",
                "intervals": [
                    {"interval": "I1", "weight": "3.000",
                     "truncated_share": "0.642", "remainder": 6000,
                     "leftover_units": 1, "residual_allocated": "0.643",
                     "fixed_total": "0.500", "allocated": "1.143",
                     "branches": [
                         {"branch": "B1", "weight": "3.000",
                          "truncated_share": "0.643", "remainder": 0,
                          "leftover_units": 0, "adjustment": "0.643"},
                     ]},
                    {"interval": "I2", "weight": "3.500",
                     "truncated_share": "0.750", "remainder": 0,
                     "leftover_units": 0, "residual_allocated": "0.750",
                     "fixed_total": "0.000", "allocated": "0.750",
                     "branches": [
                         {"branch": "B1", "weight": "2.000",
                          "truncated_share": "0.428", "remainder": 2000,
                          "leftover_units": 1, "adjustment": "0.429"},
                         {"branch": "B2", "weight": "1.500",
                          "truncated_share": "0.321", "remainder": 1500,
                          "leftover_units": 0, "adjustment": "0.321"},
                     ]},
                    {"interval": "I3", "weight": "0.500",
                     "truncated_share": "0.107", "remainder": 1000,
                     "leftover_units": 0, "residual_allocated": "0.107",
                     "fixed_total": "0.000", "allocated": "0.107",
                     "branches": [
                         {"branch": "B1", "weight": "0.500",
                          "truncated_share": "0.107", "remainder": 0,
                          "leftover_units": 0, "adjustment": "0.107"},
                         {"branch": "B2", "weight": "0.000",
                          "truncated_share": "0.000", "remainder": 0,
                          "leftover_units": 0, "adjustment": "0.000"},
                     ]},
                ],
            },
            response.text,
        )

    # 14. include_trace without branch detail is rejected with loc on
    #     include_trace and no partial results; requests that omit it never
    #     grow a calculation_trace field.
    traced_without_detail = post({**payload, "include_trace": True})
    check(
        "include_trace without branch detail rejected with loc on include_trace",
        traced_without_detail.status_code == 422
        and set(traced_without_detail.json().keys()) == {"detail"}
        and "include_trace" in traced_without_detail.json()["detail"][0]["loc"],
        traced_without_detail.text,
    )

    untraced_branch = post({**payload, "detail_level": "branch"})
    check(
        "requests without include_trace add no calculation_trace field",
        untraced_branch.status_code == 200
        and "calculation_trace" not in untraced_branch.json(),
        untraced_branch.text,
    )

    # 15. Identifier format and switch-type guards: visually identical ids
    #     padded with whitespace are rejected before allocation (never split
    #     into two bookable rows or two interval results), a padded
    #     fixed-adjustment id points at the identifier field instead of an
    #     unknown reading, and a non-boolean include_trace is rejected.
    padded_branch = post({
        "detail_level": "branch",
        "meter": {"start": "0.000", "end": "1.001"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.500"},
            {"branch": " B1", "interval": "I1", "energy": "0.500"},
        ],
    })
    check(
        "whitespace-padded branch id rejected with loc on that branch field",
        padded_branch.status_code == 422
        and set(padded_branch.json().keys()) == {"detail"}
        and padded_branch.json()["detail"][0]["loc"]
        == ["body", "readings", 1, "branch"],
        padded_branch.text,
    )

    padded_interval = post({
        "meter": {"start": "0.000", "end": "1.001"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.500"},
            {"branch": "B1", "interval": "I1 ", "energy": "0.500"},
        ],
    })
    check(
        "whitespace-padded interval id rejected with loc on that interval field",
        padded_interval.status_code == 422
        and set(padded_interval.json().keys()) == {"detail"}
        and padded_interval.json()["detail"][0]["loc"]
        == ["body", "readings", 1, "interval"],
        padded_interval.text,
    )

    padded_fixed = post({
        "detail_level": "branch",
        "meter": {"start": "0.000", "end": "1.001"},
        "readings": [{"branch": "B1", "interval": "I1", "energy": "1.000"}],
        "fixed_adjustments": [
            {"branch": " B1", "interval": "I1", "adjustment": "0.001"}
        ],
    })
    check(
        "whitespace-padded fixed adjustment id rejected at the identifier field",
        padded_fixed.status_code == 422
        and padded_fixed.json()["detail"][0]["loc"]
        == ["body", "fixed_adjustments", 0, "branch"]
        and padded_fixed.json()["detail"][0]["type"] == "value_error",
        padded_fixed.text,
    )

    numeric_trace = post({**payload, "detail_level": "branch", "include_trace": 1})
    check(
        "numeric include_trace rejected with loc on include_trace",
        numeric_trace.status_code == 422
        and set(numeric_trace.json().keys()) == {"detail"}
        and numeric_trace.json()["detail"][0]["loc"] == ["body", "include_trace"],
        numeric_trace.text,
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} acceptance check(s) failed: {FAILURES}")
        return 1
    print("\nAll acceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
