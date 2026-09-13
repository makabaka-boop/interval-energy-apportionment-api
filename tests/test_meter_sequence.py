"""Tests for the meter reading sequence derivation endpoint/service.

Every expectation is an exact fixed-point result (three-decimal strings or
raw milliunit integers); no float approximation is used anywhere.
"""

from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.allocation import to_milliunits
from app.main import app
from app.meter_sequence import derive_sequence

client = TestClient(app)
URL = "/api/v1/meter-sequences/derive"


def mixed_payload():
    # range 1000.000: first interval rolls over (990.500 -> 0.750), the
    # next three increment normally.
    return {
        "meter_id": "M-001",
        "range_max": "1000.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "990.500"},
            {"timestamp": "2026-09-02T00:00:00", "value": "0.750"},
            {"timestamp": "2026-09-03T00:00:00", "value": "11.000"},
            {"timestamp": "2026-09-04T00:00:00", "value": "11.000"},
            {"timestamp": "2026-09-05T00:00:00", "value": "12.003"},
        ],
        "segments": ["rollover", "normal", "normal", "normal"],
    }


def assert_error(response, status_code, err_type, loc_fragments):
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body.keys()) == {"detail"}  # no partial result leaks
    assert len(body["detail"]) == 1
    error = body["detail"][0]
    assert error["type"] == err_type
    for fragment in loc_fragments:
        assert fragment in error["loc"], f"{fragment} not in {error['loc']}"
    return error


# --- happy path ---------------------------------------------------------------

def test_mixed_normal_and_rollover_sequence_is_exact_per_segment():
    response = client.post(URL, json=mixed_payload())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "meter_id": "M-001",
        "range_max": "1000.000",
        "intervals": [
            {
                "index": 0,
                "start_time": "2026-09-01T00:00:00",
                "end_time": "2026-09-02T00:00:00",
                "start_value": "990.500",
                "end_value": "0.750",
                "segment_type": "rollover",
                "energy": "10.250",  # 1000.000 - 990.500 + 0.750
            },
            {
                "index": 1,
                "start_time": "2026-09-02T00:00:00",
                "end_time": "2026-09-03T00:00:00",
                "start_value": "0.750",
                "end_value": "11.000",
                "segment_type": "normal",
                "energy": "10.250",  # 11.000 - 0.750
            },
            {
                "index": 2,
                "start_time": "2026-09-03T00:00:00",
                "end_time": "2026-09-04T00:00:00",
                "start_value": "11.000",
                "end_value": "11.000",
                "segment_type": "normal",
                "energy": "0.000",  # flat normal interval
            },
            {
                "index": 3,
                "start_time": "2026-09-04T00:00:00",
                "end_time": "2026-09-05T00:00:00",
                "start_value": "11.000",
                "end_value": "12.003",
                "segment_type": "normal",
                "energy": "1.003",
            },
        ],
        "total_energy": "21.503",
    }


def test_intervals_are_sorted_by_start_time_and_carry_segment_type():
    payload = mixed_payload()
    body = client.post(URL, json=payload).json()
    start_times = [interval["start_time"] for interval in body["intervals"]]
    assert start_times == sorted(start_times)
    assert [interval["segment_type"] for interval in body["intervals"]] == [
        "rollover",
        "normal",
        "normal",
        "normal",
    ]
    # Each interval's declared bounds reproduce the segment energy.
    for interval in body["intervals"]:
        start = Decimal(interval["start_value"])
        end = Decimal(interval["end_value"])
        energy = Decimal(interval["energy"])
        if interval["segment_type"] == "normal":
            assert energy == end - start
        else:
            assert energy == Decimal(body["range_max"]) - start + end


def test_segment_energies_sum_exactly_to_total():
    body = client.post(URL, json=mixed_payload()).json()
    segment_sum = sum(
        Decimal(interval["energy"]) for interval in body["intervals"]
    )
    assert segment_sum == Decimal(body["total_energy"]) == Decimal("21.503")


def test_consecutive_rollovers_span_multiple_full_scales():
    # Two back-to-back rollovers on a 10.000 scale: total must reflect two
    # full wraps, not just the final reading.
    payload = {
        "meter_id": "M-002",
        "range_max": "10.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "9.000"},
            {"timestamp": "2026-09-02T00:00:00", "value": "8.000"},
            {"timestamp": "2026-09-03T00:00:00", "value": "7.000"},
            {"timestamp": "2026-09-04T00:00:00", "value": "8.500"},
        ],
        "segments": ["rollover", "rollover", "normal"],
    }
    body = client.post(URL, json=payload).json()
    assert [interval["energy"] for interval in body["intervals"]] == [
        "9.000",  # 10 - 9 + 8
        "9.000",  # 10 - 8 + 7
        "1.500",
    ]
    assert body["total_energy"] == "19.500"


def test_json_numbers_and_decimal_strings_are_equally_exact():
    payload = {
        "meter_id": "M-003",
        "range_max": 1.5,
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": 1.499},
            {"timestamp": "2026-09-02T00:00:00", "value": 0.001},
        ],
        "segments": ["rollover"],
    }
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["range_max"] == "1.500"
    assert body["intervals"][0]["energy"] == "0.002"
    assert body["total_energy"] == "0.002"


def test_deterministic_byte_identical_responses():
    first = client.post(URL, json=mixed_payload())
    second = client.post(URL, json=mixed_payload())
    assert first.status_code == second.status_code == 200
    assert first.content == second.content


# --- boundary readings --------------------------------------------------------

def test_zero_and_range_max_readings_are_valid():
    payload = {
        "meter_id": "M-004",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "0.000"},
            {"timestamp": "2026-09-02T00:00:00", "value": "100.000"},
        ],
        "segments": ["normal"],
    }
    body = client.post(URL, json=payload).json()
    assert body["intervals"][0]["energy"] == "100.000"
    assert body["total_energy"] == "100.000"


def test_minimum_milliunit_range_is_accepted():
    payload = {
        "meter_id": "M-005",
        "range_max": "0.001",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "0.001"},
            {"timestamp": "2026-09-02T00:00:00", "value": "0.000"},
        ],
        "segments": ["rollover"],
    }
    body = client.post(URL, json=payload).json()
    # range_max - start + end = 1 - 1 + 0 = 0 milliunits: the degenerate
    # wrap from the full-scale endpoint straight to zero.
    assert body["intervals"][0]["energy"] == "0.000"
    assert body["total_energy"] == "0.000"


def test_reading_above_range_max_points_at_sample_value():
    payload = mixed_payload()
    payload["samples"][2]["value"] = "1000.001"
    response = client.post(URL, json=payload)
    error = assert_error(
        response, 422, "reading_out_of_range", ["samples", 2, "value"]
    )
    assert "1000.001" in error["msg"]


def test_negative_reading_points_at_sample_value():
    payload = {
        "meter_id": "M-006",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "-0.001"},
            {"timestamp": "2026-09-02T00:00:00", "value": "1.000"},
        ],
        "segments": ["rollover"],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response, 422, "reading_out_of_range", ["samples", 0, "value"]
    )


# --- direction contradictions -------------------------------------------------

def test_normal_segment_with_decreasing_reading_is_located():
    payload = {
        "meter_id": "M-007",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "10.000"},
            {"timestamp": "2026-09-02T00:00:00", "value": "5.000"},
        ],
        "segments": ["normal"],
    }
    response = client.post(URL, json=payload)
    error = assert_error(
        response, 422, "segment_direction_mismatch", ["segments", 0]
    )
    assert "normal" in error["msg"] and "decreases" in error["msg"]
    # The envelope contains no intervals / partial computation.
    assert set(response.json().keys()) == {"detail"}


def test_rollover_segment_with_increasing_reading_is_located():
    payload = {
        "meter_id": "M-008",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "5.000"},
            {"timestamp": "2026-09-02T00:00:00", "value": "6.000"},
        ],
        "segments": ["rollover"],
    }
    response = client.post(URL, json=payload)
    error = assert_error(
        response, 422, "segment_direction_mismatch", ["segments", 0]
    )
    assert "rollover" in error["msg"]


def test_rollover_segment_with_equal_readings_is_located():
    payload = {
        "meter_id": "M-009",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "5.000"},
            {"timestamp": "2026-09-02T00:00:00", "value": "5.000"},
        ],
        "segments": ["rollover"],
    }
    response = client.post(URL, json=payload)
    error = assert_error(
        response, 422, "segment_direction_mismatch", ["segments", 0]
    )
    assert "equals" in error["msg"]


def test_direction_contradiction_is_located_at_the_specific_segment_index():
    payload = {
        "meter_id": "M-010",
        "range_max": "1000.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "990.000"},
            {"timestamp": "2026-09-02T00:00:00", "value": "5.000"},
            {"timestamp": "2026-09-03T00:00:00", "value": "4.000"},
            {"timestamp": "2026-09-04T00:00:00", "value": "6.000"},
        ],
        # segment 0 is a valid rollover; segment 1 wrongly declares normal
        # for a decrease; segment 2 must never be evaluated.
        "segments": ["rollover", "normal", "normal"],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response, 422, "segment_direction_mismatch", ["segments", 1]
    )


# --- ordering and structural rejections ---------------------------------------

def test_duplicate_timestamp_is_located_at_later_sample():
    payload = {
        "meter_id": "M-011",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "1.000"},
            {"timestamp": "2026-09-01T00:00:00", "value": "2.000"},
        ],
        "segments": ["normal"],
    }
    response = client.post(URL, json=payload)
    error = assert_error(
        response, 422, "duplicate_timestamp", ["samples", 1, "timestamp"]
    )
    assert "repeats" in error["msg"]


def test_reversed_timestamp_is_located_at_later_sample():
    payload = {
        "meter_id": "M-012",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-02T00:00:00", "value": "1.000"},
            {"timestamp": "2026-09-01T00:00:00", "value": "2.000"},
        ],
        "segments": ["normal"],
    }
    response = client.post(URL, json=payload)
    error = assert_error(
        response, 422, "timestamp_not_ascending", ["samples", 1, "timestamp"]
    )
    assert "precedes" in error["msg"]


def test_segments_count_mismatch_is_located_at_segments():
    payload = mixed_payload()
    payload["segments"] = ["rollover", "normal"]  # need 4 for 5 samples
    response = client.post(URL, json=payload)
    error = assert_error(
        response, 422, "segments_length_mismatch", ["segments"]
    )
    assert "4" in error["msg"] and "2" in error["msg"]


def test_invalid_segment_type_is_located_at_segment_index():
    payload = {
        "meter_id": "M-013",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "1.000"},
            {"timestamp": "2026-09-02T00:00:00", "value": "2.000"},
        ],
        "segments": ["NORMAL"],
    }
    response = client.post(URL, json=payload)
    assert_error(response, 422, "literal_error", ["segments", 0])


def test_single_sample_is_rejected():
    response = client.post(
        URL,
        json={
            "meter_id": "M-014",
            "range_max": "100.000",
            "samples": [
                {"timestamp": "2026-09-01T00:00:00", "value": "1.000"}
            ],
            "segments": [],
        },
    )
    # Both lists underflow their minimum lengths; the samples problem must
    # be located, and no derivation result may leak.
    assert response.status_code == 422
    body = response.json()
    assert set(body.keys()) == {"detail"}
    assert any(
        detail["loc"] == ["body", "samples"]
        and detail["type"] == "too_short"
        for detail in body["detail"]
    )


def test_extra_field_is_rejected_at_that_field():
    payload = {
        "meter_id": "M-015",
        "range_max": "100.000",
        "samples": [
            {"timestamp": "2026-09-01T00:00:00", "value": "1.000", "note": "x"},
            {"timestamp": "2026-09-02T00:00:00", "value": "2.000"},
        ],
        "segments": ["normal"],
    }
    response = client.post(URL, json=payload)
    assert_error(
        response, 422, "extra_forbidden", ["samples", 0, "note"]
    )


def test_range_max_zero_and_negative_are_rejected_at_range_max():
    for bad_range in ("0.000", "-1.000"):
        response = client.post(
            URL,
            json={
                "meter_id": "M-016",
                "range_max": bad_range,
                "samples": [
                    {"timestamp": "2026-09-01T00:00:00", "value": "0.000"},
                    {"timestamp": "2026-09-02T00:00:00", "value": "0.000"},
                ],
                "segments": ["normal"],
            },
        )
        assert_error(response, 422, "value_error", ["range_max"])


def test_range_max_precision_violation_is_located():
    response = client.post(
        URL,
        json={
            "meter_id": "M-017",
            "range_max": "100.0005",
            "samples": [
                {"timestamp": "2026-09-01T00:00:00", "value": "1.000"},
                {"timestamp": "2026-09-02T00:00:00", "value": "2.000"},
            ],
            "segments": ["normal"],
        },
    )
    assert_error(response, 422, "value_error", ["range_max"])


def test_blank_meter_id_is_rejected():
    payload = mixed_payload()
    payload["meter_id"] = "   "
    response = client.post(URL, json=payload)
    assert_error(response, 422, "value_error", ["meter_id"])


# --- service-level integer derivation -----------------------------------------

def _dt(hour):
    return datetime(2026, 9, 1, hour, 0, 0)


def test_service_uses_integer_milliunits_for_every_formula():
    range_max = to_milliunits(Decimal("1000.000"))
    values = [
        to_milliunits(Decimal(v))
        for v in ("990.500", "0.750", "11.000", "12.003")
    ]
    derivation = derive_sequence(
        range_max,
        values,
        [_dt(0), _dt(1), _dt(2), _dt(3)],
        ["rollover", "normal", "normal"],
    )
    assert [segment.energy for segment in derivation.segments] == [
        10250,
        10250,
        1003,
    ]
    assert derivation.total_energy == 21503
    assert derivation.segments[0].segment_type == "rollover"
    assert derivation.segments[1].segment_type == "normal"
    assert sum(s.energy for s in derivation.segments) == derivation.total_energy


def test_service_normal_subtraction_and_rollover_addition_directly():
    range_max = 5000
    derivation = derive_sequence(
        range_max,
        [100, 250, 4900, 10],
        [_dt(0), _dt(1), _dt(2), _dt(3)],
        ["normal", "normal", "rollover"],
    )
    # 250-100, 4900-250, 5000-4900+10
    assert [segment.energy for segment in derivation.segments] == [
        150,
        4650,
        110,
    ]
    assert derivation.total_energy == 4910


def test_service_rejects_direction_contradiction_without_any_segment_result():
    with pytest.raises(Exception) as excinfo:
        derive_sequence(
            1000,
            [100, 50],
            [_dt(0), _dt(1)],
            ["normal"],
        )
    error = excinfo.value
    assert error.err_type == "segment_direction_mismatch"
    assert error.loc == ["segments", 0]


def test_service_rejects_out_of_range_reading():
    with pytest.raises(Exception) as excinfo:
        derive_sequence(
            1000,
            [0, 1001],
            [_dt(0), _dt(1)],
            ["normal"],
        )
    assert excinfo.value.err_type == "reading_out_of_range"
    assert excinfo.value.loc == ["samples", 1, "value"]


def test_service_rejects_non_positive_range():
    with pytest.raises(Exception) as excinfo:
        derive_sequence(0, [0, 0], [_dt(0), _dt(1)], ["normal"])
    assert excinfo.value.err_type == "range_max_not_positive"


# --- legacy endpoint unaffected -----------------------------------------------

def test_legacy_allocation_endpoint_still_available():
    response = client.post(
        "/api/v1/settlements/allocate",
        json={
            "meter": {"start": "100.000", "end": "110.000"},
            "readings": [
                {"branch": "B1", "interval": "I1", "energy": "3.000"},
                {"branch": "B2", "interval": "I1", "energy": "1.000"},
                {"branch": "B1", "interval": "I2", "energy": "2.000"},
                {"branch": "B2", "interval": "I2", "energy": "1.500"},
                {"branch": "B1", "interval": "I3", "energy": "0.500"},
                {"branch": "B2", "interval": "I3", "energy": "0.000"},
            ],
        },
    )
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
