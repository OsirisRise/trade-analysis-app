"""Pure unit tests for the §7.4 as-of pairing (scoring.pair_as_of).

No DB — pair_as_of is a pure function over two sorted series, and the
correctness of §7.4 rests on it more than on any arithmetic. The property
under test throughout: a mark is paired with the newest spot observed AT OR
BEFORE it, never a later one.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from onchain_console.scoring import pair_as_of

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def d(value: str) -> Decimal:
    return Decimal(value)


class TestBasicMatching:
    def test_each_mark_takes_the_latest_prior_spot(self):
        marks = [(d("100"), at(1)), (d("110"), at(3)), (d("120"), at(5))]
        spots = [(d("99"), at(0)), (d("109"), at(2)), (d("119"), at(4))]

        pairs = pair_as_of(marks, spots)

        assert [(p.mark_price, p.spot_price) for p in pairs] == [
            (d("100"), d("99")),
            (d("110"), d("109")),
            (d("120"), d("119")),
        ]

    def test_spot_exactly_at_the_mark_timestamp_is_used(self):
        # "at or before" — an equal timestamp counts as known
        marks = [(d("100"), at(2))]
        spots = [(d("90"), at(1)), (d("95"), at(2))]

        (pair,) = pair_as_of(marks, spots)

        assert pair.spot_price == d("95")
        assert pair.lag == timedelta(0)


class TestNoLookahead:
    def test_a_later_spot_is_never_used_even_when_much_closer(self):
        """The mark sits 1 minute before a spot print and 3 hours after the
        previous one. Nearest-neighbour matching would grab the later
        price; as-of matching must not."""
        marks = [(d("100"), at(3))]
        spots = [(d("50"), at(0)), (d("999"), at(3.0167))]  # ~1 min later

        (pair,) = pair_as_of(marks, spots)

        assert pair.spot_price == d("50")  # not 999
        assert pair.spot_as_of == at(0)
        assert pair.lag == timedelta(hours=3)

    def test_marks_before_any_spot_are_dropped_not_back_filled(self):
        marks = [(d("100"), at(0)), (d("101"), at(1)), (d("102"), at(5))]
        spots = [(d("99"), at(4))]

        pairs = pair_as_of(marks, spots)

        # only the mark at hour 5 has a knowable spot
        assert len(pairs) == 1
        assert pairs[0].mark_price == d("102")
        assert pairs[0].captured_at == at(5)

    def test_all_marks_before_all_spots_yields_nothing(self):
        marks = [(d("100"), at(0)), (d("101"), at(1))]
        spots = [(d("99"), at(10))]

        assert pair_as_of(marks, spots) == []


class TestCarryForward:
    def test_a_stale_spot_stays_in_force_until_a_newer_one_arrives(self):
        # one spot print, many marks after it — all use it, with growing lag
        marks = [(d("100"), at(2)), (d("101"), at(26)), (d("102"), at(50))]
        spots = [(d("99"), at(1))]

        pairs = pair_as_of(marks, spots)

        assert [p.spot_price for p in pairs] == [d("99")] * 3
        assert [p.lag for p in pairs] == [
            timedelta(hours=1),
            timedelta(hours=25),
            timedelta(hours=49),
        ]

    def test_multiple_spots_between_two_marks_collapse_to_the_newest(self):
        marks = [(d("100"), at(0.5)), (d("200"), at(9))]
        spots = [
            (d("10"), at(0)),
            (d("20"), at(2)),
            (d("30"), at(4)),
            (d("40"), at(6)),
        ]

        pairs = pair_as_of(marks, spots)

        assert pairs[0].spot_price == d("10")
        assert pairs[1].spot_price == d("40")  # newest at or before hour 9
        assert len(pairs) == 2

    def test_energy_shaped_lag_is_measured_not_hidden(self):
        """EIA publishes T-2..T-6, so an energy mark is routinely paired
        with a spot several days old. The pairing is correct; the staleness
        must remain visible."""
        marks = [(d("70.5"), at(24 * 6))]
        spots = [(d("69.0"), at(0))]

        (pair,) = pair_as_of(marks, spots)

        assert pair.lag == timedelta(days=6)


class TestEdges:
    def test_empty_marks(self):
        assert pair_as_of([], [(d("1"), at(0))]) == []

    def test_empty_spots(self):
        assert pair_as_of([(d("1"), at(0))], []) == []

    def test_both_empty(self):
        assert pair_as_of([], []) == []

    def test_lag_is_never_negative(self):
        marks = [(d("100"), at(h)) for h in range(0, 20, 3)]
        spots = [(d("99"), at(h)) for h in range(0, 20, 2)]

        for pair in pair_as_of(marks, spots):
            assert pair.lag >= timedelta(0)
            assert pair.spot_as_of <= pair.captured_at

    def test_repeated_mark_timestamps_all_pair(self):
        marks = [(d("100"), at(5)), (d("101"), at(5))]
        spots = [(d("99"), at(4))]

        pairs = pair_as_of(marks, spots)

        assert len(pairs) == 2
        assert all(p.spot_price == d("99") for p in pairs)
