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

    # 6. Rejections carry field-locatable errors and no partial results.
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

    if FAILURES:
        print(f"\n{len(FAILURES)} acceptance check(s) failed: {FAILURES}")
        return 1
    print("\nAll acceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
