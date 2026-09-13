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


# --- fixed adjustments ------------------------------------------------------

def test_fixed_positive_adjustment_is_excluded_from_proportional_split():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "fixed_adjustments": [
            {"branch": "B2", "interval": "I1", "adjustment": "0.500"}
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    assert response.json() == {
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
                    {"branch": "B1", "energy": "3.000", "adjustment": "0.643",
                     "adjusted_energy": "3.643", "source": "calculated"},
                    {"branch": "B2", "energy": "1.000", "adjustment": "0.500",
                     "adjusted_energy": "1.500", "source": "fixed"},
                ],
            },
            {
                "interval": "I2",
                "branch_total": "3.500",
                "allocated": "0.750",
                "branch_allocations": [
                    {"branch": "B1", "energy": "2.000", "adjustment": "0.429",
                     "adjusted_energy": "2.429", "source": "calculated"},
                    {"branch": "B2", "energy": "1.500", "adjustment": "0.321",
                     "adjusted_energy": "1.821", "source": "calculated"},
                ],
            },
            {
                "interval": "I3",
                "branch_total": "0.500",
                "allocated": "0.107",
                "branch_allocations": [
                    {"branch": "B1", "energy": "0.500", "adjustment": "0.107",
                     "adjusted_energy": "0.607", "source": "calculated"},
                    {"branch": "B2", "energy": "0.000", "adjustment": "0.000",
                     "adjusted_energy": "0.000", "source": "calculated"},
                ],
            },
        ],
    }


def test_fixed_negative_adjustment_conserves_negative_difference():
    payload = {
        "detail_level": "branch",
        "meter": {"start": "0.000", "end": "6.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "3.000"},
            {"branch": "B2", "interval": "I1", "energy": "1.000"},
            {"branch": "B1", "interval": "I2", "energy": "2.000"},
            {"branch": "B2", "interval": "I2", "energy": "1.500"},
            {"branch": "B1", "interval": "I3", "energy": "0.500"},
            {"branch": "B2", "interval": "I3", "energy": "0.000"},
        ],
        "fixed_adjustments": [
            {"branch": "B2", "interval": "I1", "adjustment": "-0.500"}
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["difference"] == "-2.000"
    assert body["check_sum"] == "-2.000"
    assert body["allocations"] == [
        {
            "interval": "I1",
            "branch_total": "4.000",
            "allocated": "-1.143",
            "branch_allocations": [
                {"branch": "B1", "energy": "3.000", "adjustment": "-0.643",
                 "adjusted_energy": "2.357", "source": "calculated"},
                {"branch": "B2", "energy": "1.000", "adjustment": "-0.500",
                 "adjusted_energy": "0.500", "source": "fixed"},
            ],
        },
        {
            "interval": "I2",
            "branch_total": "3.500",
            "allocated": "-0.750",
            "branch_allocations": [
                {"branch": "B1", "energy": "2.000", "adjustment": "-0.429",
                 "adjusted_energy": "1.571", "source": "calculated"},
                {"branch": "B2", "energy": "1.500", "adjustment": "-0.321",
                 "adjusted_energy": "1.179", "source": "calculated"},
            ],
        },
        {
            "interval": "I3",
            "branch_total": "0.500",
            "allocated": "-0.107",
            "branch_allocations": [
                {"branch": "B1", "energy": "0.500", "adjustment": "-0.107",
                 "adjusted_energy": "0.393", "source": "calculated"},
                {"branch": "B2", "energy": "0.000", "adjustment": "0.000",
                 "adjusted_energy": "0.000", "source": "calculated"},
            ],
        },
    ]


def test_explicit_empty_fixed_adjustments_labels_every_branch_calculated():
    payload = {**base_payload(), "detail_level": "branch", "fixed_adjustments": []}
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    for interval in response.json()["allocations"]:
        assert [row["source"] for row in interval["branch_allocations"]] == [
            "calculated"
        ] * len(interval["branch_allocations"])


def test_fixed_adjustments_requires_branch_detail():
    payload = {
        **base_payload(),
        "fixed_adjustments": [
            {"branch": "B1", "interval": "I1", "adjustment": "0.001"}
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response,
        422,
        "fixed_adjustments_requires_branch_detail",
        ["fixed_adjustments"],
    )


def test_duplicate_fixed_adjustment_is_rejected_at_item_index():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "fixed_adjustments": [
            {"branch": "B1", "interval": "I1", "adjustment": "0.100"},
            {"branch": "B1", "interval": "I1", "adjustment": "0.200"},
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response, 422, "duplicate_fixed_adjustment", ["fixed_adjustments", 1]
    )


def test_unknown_fixed_adjustment_reading_is_rejected_at_item_index():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "fixed_adjustments": [
            {"branch": "B9", "interval": "I1", "adjustment": "0.100"}
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response, 422, "unknown_fixed_adjustment", ["fixed_adjustments", 0]
    )


def test_fixed_adjustment_with_opposite_sign_is_rejected_at_adjustment_field():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "fixed_adjustments": [
            {"branch": "B1", "interval": "I1", "adjustment": "-0.001"}
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response,
        422,
        "fixed_adjustment_sign_mismatch",
        ["fixed_adjustments", 0, "adjustment"],
    )


def test_fixed_adjustment_absolute_total_exceeding_difference_is_rejected():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "fixed_adjustments": [
            {"branch": "B1", "interval": "I1", "adjustment": "1.000"},
            {"branch": "B2", "interval": "I1", "adjustment": "1.001"},
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response,
        422,
        "fixed_adjustment_total_exceeds_difference",
        ["fixed_adjustments", 1, "adjustment"],
    )


def test_fixed_adjustment_absolute_total_equal_difference_is_allowed():
    payload = {
        "detail_level": "branch",
        "meter": {"start": "0.000", "end": "1.001"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.000"},
            {"branch": "B2", "interval": "I1", "energy": "1.000"},
        ],
        "fixed_adjustments": [
            {"branch": "B2", "interval": "I1", "adjustment": "0.001"}
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["difference"] == "0.001"
    assert body["check_sum"] == "0.001"
    assert body["allocations"] == [
        {
            "interval": "I1",
            "branch_total": "1.000",
            "allocated": "0.001",
            "branch_allocations": [
                {"branch": "B1", "energy": "0.000", "adjustment": "0.000",
                 "adjusted_energy": "0.000", "source": "calculated"},
                {"branch": "B2", "energy": "1.000", "adjustment": "0.001",
                 "adjusted_energy": "1.001", "source": "fixed"},
            ],
        }
    ]


def test_nonzero_fixed_adjustment_is_rejected_when_difference_is_zero():
    payload = {
        "detail_level": "branch",
        "meter": {"start": "0.000", "end": "1.000"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "1.000"},
        ],
        "fixed_adjustments": [
            {"branch": "B1", "interval": "I1", "adjustment": "0.001"}
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response,
        422,
        "fixed_adjustment_sign_mismatch",
        ["fixed_adjustments", 0, "adjustment"],
    )


def test_nonzero_residual_with_only_locked_reading_points_at_fixed_item():
    payload = {
        "detail_level": "branch",
        "meter": {"start": "0.000", "end": "0.001"},
        "readings": [{"branch": "B1", "interval": "I1", "energy": "0.000"}],
        "fixed_adjustments": [
            {"branch": "B1", "interval": "I1", "adjustment": "0.000"}
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response, 422, "zero_unlocked_weight", ["fixed_adjustments", 0]
    )


def test_nonzero_residual_with_zero_unlocked_reading_points_at_reading():
    payload = {
        "detail_level": "branch",
        "meter": {"start": "0.000", "end": "0.001"},
        "readings": [
            {"branch": "B1", "interval": "I1", "energy": "0.000"},
            {"branch": "B2", "interval": "I1", "energy": "0.000"},
        ],
        "fixed_adjustments": [
            {"branch": "B1", "interval": "I1", "adjustment": "0.000"}
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(response, 422, "zero_unlocked_weight", ["readings", 1])


def test_fixed_adjustment_precision_error_is_located():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "fixed_adjustments": [
            {"branch": "B1", "interval": "I1", "adjustment": "0.0005"}
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response,
        422,
        "value_error",
        ["fixed_adjustments", 0, "adjustment"],
    )


def test_extra_fixed_adjustment_field_is_rejected():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "fixed_adjustments": [
            {
                "branch": "B1",
                "interval": "I1",
                "adjustment": "0.001",
                "comment": "locked",
            }
        ],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response,
        422,
        "extra_forbidden",
        ["fixed_adjustments", 0, "comment"],
    )


# --- calculation trace ------------------------------------------------------

def trace_payload():
    # difference 0.007 over interval weights 5/3/2: every interval truncates
    # with a non-zero remainder and one leftover milliunit must be placed.
    return {
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


def test_trace_records_exact_positive_remainder_fill_in():
    response = client.post(URL, json=trace_payload())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "meter_increment": "0.017",
        "branch_total": "0.010",
        "difference": "0.007",
        "check_sum": "0.007",
        "allocations": [
            {
                "interval": "I1",
                "branch_total": "0.005",
                "allocated": "0.004",
                "branch_allocations": [
                    {"branch": "B1", "energy": "0.003", "adjustment": "0.002",
                     "adjusted_energy": "0.005"},
                    {"branch": "B2", "energy": "0.002", "adjustment": "0.002",
                     "adjusted_energy": "0.004"},
                ],
            },
            {
                "interval": "I2",
                "branch_total": "0.003",
                "allocated": "0.002",
                "branch_allocations": [
                    {"branch": "B1", "energy": "0.003", "adjustment": "0.002",
                     "adjusted_energy": "0.005"},
                    {"branch": "B2", "energy": "0.000", "adjustment": "0.000",
                     "adjusted_energy": "0.000"},
                ],
            },
            {
                "interval": "I3",
                "branch_total": "0.002",
                "allocated": "0.001",
                "branch_allocations": [
                    {"branch": "B1", "energy": "0.002", "adjustment": "0.001",
                     "adjusted_energy": "0.003"},
                    {"branch": "B2", "energy": "0.000", "adjustment": "0.000",
                     "adjusted_energy": "0.000"},
                ],
            },
        ],
        "calculation_trace": {
            "difference": "0.007",
            "fixed_total": "0.000",
            "residual_difference": "0.007",
            "intervals": [
                {
                    # 7*5/10 = 3 r5: largest remainder earns the leftover unit
                    "interval": "I1",
                    "weight": "0.005",
                    "truncated_share": "0.003",
                    "remainder": 5,
                    "leftover_units": 1,
                    "residual_allocated": "0.004",
                    "fixed_total": "0.000",
                    "allocated": "0.004",
                    "branches": [
                        {"branch": "B1", "weight": "0.003",
                         "truncated_share": "0.002", "remainder": 2,
                         "leftover_units": 0, "adjustment": "0.002"},
                        # 4*2/5 = 1 r3 beats B1's r2: B2 gets the unit
                        {"branch": "B2", "weight": "0.002",
                         "truncated_share": "0.001", "remainder": 3,
                         "leftover_units": 1, "adjustment": "0.002"},
                    ],
                },
                {
                    "interval": "I2",
                    "weight": "0.003",
                    "truncated_share": "0.002",
                    "remainder": 1,
                    "leftover_units": 0,
                    "residual_allocated": "0.002",
                    "fixed_total": "0.000",
                    "allocated": "0.002",
                    "branches": [
                        {"branch": "B1", "weight": "0.003",
                         "truncated_share": "0.002", "remainder": 0,
                         "leftover_units": 0, "adjustment": "0.002"},
                        {"branch": "B2", "weight": "0.000",
                         "truncated_share": "0.000", "remainder": 0,
                         "leftover_units": 0, "adjustment": "0.000"},
                    ],
                },
                {
                    "interval": "I3",
                    "weight": "0.002",
                    "truncated_share": "0.001",
                    "remainder": 4,
                    "leftover_units": 0,
                    "residual_allocated": "0.001",
                    "fixed_total": "0.000",
                    "allocated": "0.001",
                    "branches": [
                        {"branch": "B1", "weight": "0.002",
                         "truncated_share": "0.001", "remainder": 0,
                         "leftover_units": 0, "adjustment": "0.001"},
                        {"branch": "B2", "weight": "0.000",
                         "truncated_share": "0.000", "remainder": 0,
                         "leftover_units": 0, "adjustment": "0.000"},
                    ],
                },
            ],
        },
    }


def test_trace_records_exact_negative_remainder_fill_in():
    payload = trace_payload()
    payload["meter"] = {"start": "0.000", "end": "0.003"}  # difference -0.007
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["difference"] == "-0.007"
    assert body["check_sum"] == "-0.007"
    assert body["calculation_trace"] == {
        "difference": "-0.007",
        "fixed_total": "0.000",
        "residual_difference": "-0.007",
        "intervals": [
            {
                # same remainder ranking, but the leftover unit is negative
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
            {
                "interval": "I2",
                "weight": "0.003",
                "truncated_share": "-0.002",
                "remainder": 1,
                "leftover_units": 0,
                "residual_allocated": "-0.002",
                "fixed_total": "0.000",
                "allocated": "-0.002",
                "branches": [
                    {"branch": "B1", "weight": "0.003",
                     "truncated_share": "-0.002", "remainder": 0,
                     "leftover_units": 0, "adjustment": "-0.002"},
                    {"branch": "B2", "weight": "0.000",
                     "truncated_share": "0.000", "remainder": 0,
                     "leftover_units": 0, "adjustment": "0.000"},
                ],
            },
            {
                "interval": "I3",
                "weight": "0.002",
                "truncated_share": "-0.001",
                "remainder": 4,
                "leftover_units": 0,
                "residual_allocated": "-0.001",
                "fixed_total": "0.000",
                "allocated": "-0.001",
                "branches": [
                    {"branch": "B1", "weight": "0.002",
                     "truncated_share": "-0.001", "remainder": 0,
                     "leftover_units": 0, "adjustment": "-0.001"},
                    {"branch": "B2", "weight": "0.000",
                     "truncated_share": "0.000", "remainder": 0,
                     "leftover_units": 0, "adjustment": "0.000"},
                ],
            },
        ],
    }


def test_trace_with_fixed_adjustments_explains_residual_and_keeps_locked_share():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "include_trace": True,
        "fixed_adjustments": [
            {"branch": "B2", "interval": "I1", "adjustment": "0.500"}
        ],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    # The formal result is unchanged by tracing.
    assert [item["allocated"] for item in body["allocations"]] == [
        "1.143", "0.750", "0.107",
    ]
    assert body["calculation_trace"] == {
        "difference": "2.000",
        "fixed_total": "0.500",
        "residual_difference": "1.500",
        "intervals": [
            {
                # unlocked weight only: B2 is locked out of the residual split
                "interval": "I1",
                "weight": "3.000",
                "truncated_share": "0.642",
                "remainder": 6000,
                "leftover_units": 1,
                "residual_allocated": "0.643",
                # the locked adjustment still contributes to the interval
                "fixed_total": "0.500",
                "allocated": "1.143",
                # only residual-calculation items are explained per branch
                "branches": [
                    {"branch": "B1", "weight": "3.000",
                     "truncated_share": "0.643", "remainder": 0,
                     "leftover_units": 0, "adjustment": "0.643"},
                ],
            },
            {
                "interval": "I2",
                "weight": "3.500",
                "truncated_share": "0.750",
                "remainder": 0,
                "leftover_units": 0,
                "residual_allocated": "0.750",
                "fixed_total": "0.000",
                "allocated": "0.750",
                "branches": [
                    {"branch": "B1", "weight": "2.000",
                     "truncated_share": "0.428", "remainder": 2000,
                     "leftover_units": 1, "adjustment": "0.429"},
                    {"branch": "B2", "weight": "1.500",
                     "truncated_share": "0.321", "remainder": 1500,
                     "leftover_units": 0, "adjustment": "0.321"},
                ],
            },
            {
                "interval": "I3",
                "weight": "0.500",
                "truncated_share": "0.107",
                "remainder": 1000,
                "leftover_units": 0,
                "residual_allocated": "0.107",
                "fixed_total": "0.000",
                "allocated": "0.107",
                "branches": [
                    {"branch": "B1", "weight": "0.500",
                     "truncated_share": "0.107", "remainder": 0,
                     "leftover_units": 0, "adjustment": "0.107"},
                    # B2 is locked only in I1; here it stays a residual item
                    {"branch": "B2", "weight": "0.000",
                     "truncated_share": "0.000", "remainder": 0,
                     "leftover_units": 0, "adjustment": "0.000"},
                ],
            },
        ],
    }


def test_trace_reconciliation_chain_holds_for_every_interval():
    payload = {
        **base_payload(),
        "detail_level": "branch",
        "include_trace": True,
        "fixed_adjustments": [
            {"branch": "B2", "interval": "I1", "adjustment": "0.500"}
        ],
    }
    body = client.post(URL, json=payload).json()
    trace = body["calculation_trace"]
    assert (
        Decimal(trace["fixed_total"]) + Decimal(trace["residual_difference"])
        == Decimal(trace["difference"])
    )
    for interval in trace["intervals"]:
        assert (
            Decimal(interval["truncated_share"])
            + Decimal(interval["leftover_units"]) * Decimal("0.001")
            == Decimal(interval["residual_allocated"])
        )
        assert (
            Decimal(interval["residual_allocated"])
            + Decimal(interval["fixed_total"])
            == Decimal(interval["allocated"])
        )
        branch_sum = sum(Decimal(b["adjustment"]) for b in interval["branches"])
        assert branch_sum == Decimal(interval["residual_allocated"])
    interval_sum = sum(Decimal(i["allocated"]) for i in trace["intervals"])
    assert interval_sum == Decimal(trace["difference"]) == Decimal(body["check_sum"])


def test_include_trace_without_branch_detail_is_rejected_at_include_trace():
    for variant in (
        {**base_payload(), "include_trace": True},
        {**base_payload(), "detail_level": "interval", "include_trace": True},
    ):
        response = client.post(URL, json=variant)
        error = assert_error(
            response, 422, "include_trace_requires_branch_detail", ["include_trace"]
        )
        assert "branch" in error["msg"]


def test_include_trace_failure_returns_no_partial_trace():
    payload = trace_payload()
    payload["readings"][0]["energy"] = "0.0005"  # precision violation
    response = client.post(URL, json=payload)
    assert_error(response, 422, "value_error", ["readings", 0, "energy"])


def test_request_without_include_trace_adds_no_trace_field():
    for variant in (
        base_payload(),
        {**base_payload(), "detail_level": "branch"},
        {**base_payload(), "detail_level": "branch", "include_trace": False},
    ):
        response = client.post(URL, json=variant)
        assert response.status_code == 200, response.text
        assert "calculation_trace" not in response.json()


def test_explicit_false_include_trace_is_byte_identical_to_omitted():
    with_false = client.post(
        URL, json={**base_payload(), "detail_level": "branch", "include_trace": False}
    )
    omitted = client.post(URL, json={**base_payload(), "detail_level": "branch"})
    assert with_false.status_code == omitted.status_code == 200
    assert with_false.content == omitted.content


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


def test_all_whitespace_branch_id_is_rejected_before_allocation():
    payload = base_payload()
    payload["readings"][0]["branch"] = "   "
    response = client.post(URL, json=payload)
    error = assert_error(response, 422, "value_error", ["readings", 0, "branch"])
    assert "whitespace" in error["msg"]


def test_all_whitespace_interval_id_is_rejected_before_allocation():
    payload = base_payload()
    payload["readings"][1]["interval"] = " \t "
    response = client.post(URL, json=payload)
    error = assert_error(response, 422, "value_error", ["readings", 1, "interval"])
    assert "whitespace" in error["msg"]


def test_misspelled_detail_level_field_is_rejected():
    # "detail_leve" must not silently fall back to interval-level results.
    payload = {**base_payload(), "detail_leve": "branch"}
    response = client.post(URL, json=payload)
    assert_error(response, 422, "extra_forbidden", ["detail_leve"])


def test_misspelled_extra_reading_field_is_rejected():
    # The misspelled "enery" rides alongside a valid "energy"; it must be
    # rejected and located, not silently ignored.
    payload = base_payload()
    payload["readings"][0]["enery"] = "9.999"
    response = client.post(URL, json=payload)
    assert_error(response, 422, "extra_forbidden", ["readings", 0, "enery"])


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
