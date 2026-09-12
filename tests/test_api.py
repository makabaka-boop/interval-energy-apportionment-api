"""API tests: exact responses, error envelopes, determinism.

Every expectation is an exact string (three-decimal fixed point); no float
approximation, no mocked computation.
"""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
URL = "/api/v1/settlements/allocate"


def base_payload():
    return {
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


# --- happy path -------------------------------------------------------------

def test_full_response_is_exact_and_sorted_by_interval():
    response = client.post(URL, json=base_payload())
    assert response.status_code == 200
    assert response.json() == {
        "meter_increment": "10.000",
        "branch_total": "8.000",
        "difference": "2.000",
        "check_sum": "2.000",
        "allocations": [
            {"interval": "I1", "branch_total": "4.000", "allocated": "1.000"},
            {"interval": "I2", "branch_total": "3.500", "allocated": "0.875"},
            {"interval": "I3", "branch_total": "0.500", "allocated": "0.125"},
        ],
    }


def test_cancelling_branches_keep_interval_weight():
    payload = {
        "meter": {"start": "0.000", "end": "3.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "4.000"},
            {"branch": "B2", "interval": "I1", "energy": "-4.000"},
            {"branch": "B1", "interval": "I2", "energy": "2.000"},
            {"branch": "B2", "interval": "I2", "energy": "0.000"},
        ],
    }
    # branch total 2.000 -> difference 1.000; I1 nets to 0 but its weight is
    # |4.000| + |-4.000| = 8.000 vs I2's 2.000, so the split is 0.800 / 0.200.
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["difference"] == "1.000"
    assert body["check_sum"] == "1.000"
    assert body["allocations"] == [
        {"interval": "I1", "branch_total": "0.000", "allocated": "0.800"},
        {"interval": "I2", "branch_total": "2.000", "allocated": "0.200"},
    ]


def test_negative_interval_total_uses_absolute_weight():
    payload = {
        "meter": {"start": "0.000", "end": "2.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "-4.000"},
            {"branch": "B1", "interval": "I2", "energy": "3.500"},
            {"branch": "B1", "interval": "I3", "energy": "0.500"},
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["branch_total"] == "0.000"
    assert body["difference"] == "2.000"
    assert body["check_sum"] == "2.000"
    assert body["allocations"] == [
        {"interval": "I1", "branch_total": "-4.000", "allocated": "1.000"},
        {"interval": "I2", "branch_total": "3.500", "allocated": "0.875"},
        {"interval": "I3", "branch_total": "0.500", "allocated": "0.125"},
    ]


def test_tie_order_and_response_sorting_over_the_wire():
    payload = {
        "meter": {"start": "0.000", "end": "3.002"},
        "readings": [
            {"branch": "B1", "interval": "I3", "energy": "1.000"},
            {"branch": "B1", "interval": "I1", "energy": "1.000"},
            {"branch": "B1", "interval": "I2", "energy": "1.000"},
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    body = response.json()
    # equal remainders -> lexicographic id order; response sorted by interval
    assert body["allocations"] == [
        {"interval": "I1", "branch_total": "1.000", "allocated": "0.001"},
        {"interval": "I2", "branch_total": "1.000", "allocated": "0.001"},
        {"interval": "I3", "branch_total": "1.000", "allocated": "0.000"},
    ]
    assert body["check_sum"] == "0.002"


def test_negative_difference_over_the_wire():
    payload = {
        "meter": {"start": "10.000", "end": "12.998"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "1.000"},
            {"branch": "B1", "interval": "I2", "energy": "1.000"},
            {"branch": "B1", "interval": "I3", "energy": "1.000"},
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["difference"] == "-0.002"
    assert body["check_sum"] == "-0.002"
    assert body["allocations"] == [
        {"interval": "I1", "branch_total": "1.000", "allocated": "-0.001"},
        {"interval": "I2", "branch_total": "1.000", "allocated": "-0.001"},
        {"interval": "I3", "branch_total": "1.000", "allocated": "0.000"},
    ]


def test_json_numbers_are_accepted_and_exact():
    payload = {
        "meter": {"start": 0, "end": 0.002},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": 0.001},
            {"branch": "B1", "interval": "I2", "energy": 0},
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["difference"] == "0.001"
    assert body["allocations"] == [
        {"interval": "I1", "branch_total": "0.001", "allocated": "0.001"},
        {"interval": "I2", "branch_total": "0.000", "allocated": "0.000"},
    ]


def test_same_request_always_yields_identical_response():
    first = client.post(URL, json=base_payload())
    second = client.post(URL, json=base_payload())
    assert first.status_code == second.status_code == 200
    assert first.content == second.content


# --- detail_level -----------------------------------------------------------

def test_branch_detail_adds_bookable_per_branch_breakdown():
    payload = {**base_payload(), "detail_level": "branch"}
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    assert response.json() == {
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
                    {"branch": "B1", "energy": "3.000", "adjustment": "0.750",
                     "adjusted_energy": "3.750"},
                    {"branch": "B2", "energy": "1.000", "adjustment": "0.250",
                     "adjusted_energy": "1.250"},
                ],
            },
            {
                "interval": "I2",
                "branch_total": "3.500",
                "allocated": "0.875",
                "branch_allocations": [
                    {"branch": "B1", "energy": "2.000", "adjustment": "0.500",
                     "adjusted_energy": "2.500"},
                    {"branch": "B2", "energy": "1.500", "adjustment": "0.375",
                     "adjusted_energy": "1.875"},
                ],
            },
            {
                "interval": "I3",
                "branch_total": "0.500",
                "allocated": "0.125",
                "branch_allocations": [
                    {"branch": "B1", "energy": "0.500", "adjustment": "0.125",
                     "adjusted_energy": "0.625"},
                    {"branch": "B2", "energy": "0.000", "adjustment": "0.000",
                     "adjusted_energy": "0.000"},
                ],
            },
        ],
    }


def test_branch_detail_two_level_conservation_holds_per_interval():
    payload = {**base_payload(), "detail_level": "branch"}
    body = client.post(URL, json=payload).json()
    for item in body["allocations"]:
        adjustments = sum(
            Decimal(b["adjustment"]) for b in item["branch_allocations"]
        )
        assert adjustments == Decimal(item["allocated"])
        for branch in item["branch_allocations"]:
            assert (
                Decimal(branch["energy"]) + Decimal(branch["adjustment"])
                == Decimal(branch["adjusted_energy"])
            )


def test_branch_detail_negative_difference_and_tied_branch_order():
    payload = {
        "detail_level": "branch",
        "meter": {"start": "1.000", "end": "1.997"},
        "readings": [
            # B2 submitted first; ties must still resolve by branch id.
            {"branch": "B2", "interval": "I1", "energy": "0.500"},
            {"branch": "B1", "interval": "I1", "energy": "0.500"},
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["difference"] == "-0.003"
    assert body["check_sum"] == "-0.003"
    # -3 milliunits over equal weights: -1 each after truncation, the last
    # negative unit goes to the lexicographically first branch (B1).
    assert body["allocations"] == [
        {
            "interval": "I1",
            "branch_total": "1.000",
            "allocated": "-0.003",
            "branch_allocations": [
                {"branch": "B1", "energy": "0.500", "adjustment": "-0.002",
                 "adjusted_energy": "0.498"},
                {"branch": "B2", "energy": "0.500", "adjustment": "-0.001",
                 "adjusted_energy": "0.499"},
            ],
        }
    ]


def test_invalid_detail_level_is_rejected_with_field_location():
    payload = {**base_payload(), "detail_level": "daily"}
    response = client.post(URL, json=payload)
    error = assert_error(response, 422, "literal_error", ["detail_level"])
    assert "interval" in error["msg"] and "branch" in error["msg"]


def test_omitted_detail_level_keeps_exact_legacy_response():
    legacy = client.post(URL, json=base_payload())
    explicit_interval = client.post(
        URL, json={**base_payload(), "detail_level": "interval"}
    )
    assert legacy.status_code == explicit_interval.status_code == 200
    assert explicit_interval.content == legacy.content
    body = legacy.json()
    assert body == {
        "meter_increment": "10.000",
        "branch_total": "8.000",
        "difference": "2.000",
        "check_sum": "2.000",
        "allocations": [
            {"interval": "I1", "branch_total": "4.000", "allocated": "1.000"},
            {"interval": "I2", "branch_total": "3.500", "allocated": "0.875"},
            {"interval": "I3", "branch_total": "0.500", "allocated": "0.125"},
        ],
    }
    assert all("branch_allocations" not in item for item in body["allocations"])


# --- rejections: field-locatable errors, no partial results ------------------

def assert_error(response, status_code, err_type, loc_fragments):
    assert response.status_code == status_code
    body = response.json()
    assert set(body.keys()) == {"detail"}  # no partial result leaks
    assert len(body["detail"]) == 1
    error = body["detail"][0]
    assert error["type"] == err_type
    for fragment in loc_fragments:
        assert fragment in error["loc"], f"{fragment} not in {error['loc']}"
    return error


def test_precision_out_of_bounds_points_at_energy_field():
    payload = base_payload()
    payload["readings"][2]["energy"] = "2.0005"
    response = client.post(URL, json=payload)
    error = assert_error(response, 422, "value_error", ["readings", 2, "energy"])
    assert "0.001" in error["msg"]


def test_precision_out_of_bounds_on_meter_reading():
    payload = base_payload()
    payload["meter"]["start"] = "99.9999"
    response = client.post(URL, json=payload)
    assert_error(response, 422, "value_error", ["meter", "start"])


def test_backwards_meter_reading_is_rejected():
    payload = base_payload()
    payload["meter"] = {"start": "110.000", "end": "100.000"}
    response = client.post(URL, json=payload)
    error = assert_error(response, 422, "value_error", ["meter"])
    assert "backwards" in error["msg"]


def test_interval_set_mismatch_is_rejected():
    payload = {
        "meter": {"start": "0.000", "end": "1.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.500"},
            {"branch": "B1", "interval": "I2", "energy": "0.200"},
            {"branch": "B2", "interval": "I1", "energy": "0.100"},
        ],
    }
    response = client.post(URL, json=payload)
    error = assert_error(response, 422, "interval_set_mismatch", ["readings"])
    assert "B2" in error["msg"] and "I2" in error["msg"]


def test_duplicate_branch_interval_is_rejected_with_index():
    payload = {
        "meter": {"start": "0.000", "end": "1.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.500"},
            {"branch": "B1", "interval": "I1", "energy": "0.250"},
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(response, 422, "duplicate_reading", ["readings", 1])


def test_zero_total_weight_with_nonzero_difference_is_rejected():
    payload = {
        "meter": {"start": "0.000", "end": "0.001"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.000"},
            {"branch": "B1", "interval": "I2", "energy": "0.000"},
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(response, 422, "zero_total_weight", ["readings"])


def test_zero_total_weight_with_zero_difference_is_accepted():
    payload = {
        "meter": {"start": "5.000", "end": "5.000"},
        "readings": [{"branch": "B1", "interval": "I1", "energy": "0.000"}],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["difference"] == "0.000"
    assert body["check_sum"] == "0.000"
    assert body["allocations"] == [
        {"interval": "I1", "branch_total": "0.000", "allocated": "0.000"}
    ]


def test_empty_readings_list_is_rejected():
    response = client.post(
        URL, json={"meter": {"start": "0.000", "end": "1.000"}, "readings": []}
    )
    assert_error(response, 422, "too_short", ["readings"])


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
