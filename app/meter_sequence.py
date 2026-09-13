"""Interval-energy derivation from a time-ordered cumulative meter sequence.

The park collection system only stores cumulative meter indications
("readings"). Between two adjacent samples the clerk declares whether the
meter incremented normally or rolled over the full scale:

* ``normal``:   energy = end - start
* ``rollover``: energy = range_max - start + end

Every quantity here is an integer count of milliunits (1 milliunit =
0.001 kWh); the derivation never touches binary floating point. A legal
rollover is exactly the case where the raw indication *decreases*, so a
declared segment type that contradicts the observed direction is rejected
and located at the offending segment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence

from .allocation import format_milliunits

NORMAL_SEGMENT = "normal"
ROLLOVER_SEGMENT = "rollover"

SegmentType = Literal["normal", "rollover"]


class SequenceError(Exception):
    """Business-rule rejection for a meter sequence.

    ``loc`` is relative to the request body (the route prepends ``"body"``),
    so every failure points at the concrete sample field or segment index:
    e.g. ``["samples", 2, "value"]`` or ``["segments", 0]``.
    """

    def __init__(self, loc: list, msg: str, err_type: str) -> None:
        super().__init__(msg)
        self.loc = loc
        self.msg = msg
        self.err_type = err_type


@dataclass(frozen=True)
class SegmentEnergy:
    """One derived interval between two adjacent samples.

    All values are the exact integers used by the fixed-point computation:
    ``start_value``/``end_value``/``energy`` in milliunits.
    """

    index: int
    start_time: datetime
    end_time: datetime
    start_value: int
    end_value: int
    segment_type: SegmentType
    energy: int


@dataclass(frozen=True)
class SequenceDerivation:
    """All interval results plus the whole-sequence total in milliunits."""

    range_max: int
    segments: tuple[SegmentEnergy, ...]
    total_energy: int


def derive_sequence(
    range_max: int,
    values: Sequence[int],
    timestamps: Sequence[datetime],
    segment_types: Sequence[SegmentType],
) -> SequenceDerivation:
    """Derive every interval's energy in milliunits.

    Either the whole sequence is returned or a :class:`SequenceError` is
    raised; partial intervals are never produced. Validation order is fixed
    so the same request always yields the same located error:

    1. positive range, strictly ascending tz-consistent timestamps;
    2. every reading inside ``[0, range_max]``;
    3. one segment declaration per adjacent sample pair;
    4. per segment, declared type matches the observed reading direction.
    """
    count = len(values)
    if len(timestamps) != count:
        raise AssertionError("one timestamp is required per sample")

    if range_max <= 0:
        raise SequenceError(
            ["range_max"],
            f"range_max {format_milliunits(range_max)} must be greater than "
            "zero (smallest positive range is 0.001 kWh)",
            "range_max_not_positive",
        )

    # Strict chronological order. Mixing timezone-aware and naive timestamps
    # would make ordering ambiguous (and comparison raise), so reject it at
    # the first sample that introduces the mismatch.
    for index in range(1, count):
        previous = timestamps[index - 1]
        current = timestamps[index]
        if (previous.tzinfo is None) != (current.tzinfo is None):
            raise SequenceError(
                ["samples", index, "timestamp"],
                f"sample {index} mixes timezone-aware and naive timestamps; "
                "every timestamp in one request must use the same convention",
                "timestamp_timezone_mismatch",
            )
        if current == previous:
            raise SequenceError(
                ["samples", index, "timestamp"],
                f"sample {index} repeats the timestamp of sample {index - 1} "
                f"({current.isoformat()}); timestamps must be strictly "
                "ascending",
                "duplicate_timestamp",
            )
        if current < previous:
            raise SequenceError(
                ["samples", index, "timestamp"],
                f"sample {index} timestamp {current.isoformat()} precedes "
                f"sample {index - 1} timestamp {previous.isoformat()}; "
                "samples must be arranged in strictly ascending time order",
                "timestamp_not_ascending",
            )

    for index, value in enumerate(values):
        if value < 0 or value > range_max:
            raise SequenceError(
                ["samples", index, "value"],
                f"meter reading {format_milliunits(value)} at sample {index} "
                f"is outside the meter range [0.000, "
                f"{format_milliunits(range_max)}]",
                "reading_out_of_range",
            )

    expected_segments = count - 1
    if len(segment_types) != expected_segments:
        raise SequenceError(
            ["segments"],
            f"expected {expected_segments} segment declaration(s) for "
            f"{count} samples (one per adjacent sample pair), got "
            f"{len(segment_types)}",
            "segments_length_mismatch",
        )

    segments: list[SegmentEnergy] = []
    total_energy = 0
    for index, segment_type in enumerate(segment_types):
        start_value = values[index]
        end_value = values[index + 1]
        if segment_type == NORMAL_SEGMENT:
            if end_value < start_value:
                raise SequenceError(
                    ["segments", index],
                    f"segment {index} is declared 'normal' but the reading "
                    f"decreases from {format_milliunits(start_value)} to "
                    f"{format_milliunits(end_value)}; a decreasing interval "
                    "must be declared 'rollover'",
                    "segment_direction_mismatch",
                )
            energy = end_value - start_value
        else:
            # A rollover is legal exactly when the indication drops; an
            # ascending or flat reading cannot be a scale wrap.
            if end_value >= start_value:
                relation = "equals" if end_value == start_value else "exceeds"
                raise SequenceError(
                    ["segments", index],
                    f"segment {index} is declared 'rollover' but the end "
                    f"reading {format_milliunits(end_value)} {relation} the "
                    f"start reading {format_milliunits(start_value)}; a "
                    "rollover requires the end reading to be smaller than "
                    "the start reading",
                    "segment_direction_mismatch",
                )
            energy = range_max - start_value + end_value

        total_energy += energy
        segments.append(
            SegmentEnergy(
                index=index,
                start_time=timestamps[index],
                end_time=timestamps[index + 1],
                start_value=start_value,
                end_value=end_value,
                segment_type=segment_type,
                energy=energy,
            )
        )

    derivation = SequenceDerivation(
        range_max=range_max,
        segments=tuple(segments),
        total_energy=total_energy,
    )
    # Internal invariant: the total is accumulated from the same integer
    # segment energies, so they can never disagree.
    if sum(segment.energy for segment in derivation.segments) != total_energy:
        raise AssertionError("segment energies do not sum to the total")
    return derivation
