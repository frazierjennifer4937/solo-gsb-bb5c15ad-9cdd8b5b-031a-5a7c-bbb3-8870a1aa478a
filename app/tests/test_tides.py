import math
from datetime import datetime, timezone

from app.services.tides import (
    _clip_merge_blocks,
    compute_navigation_windows,
)


def dt(hours: float) -> datetime:
    return datetime.fromtimestamp(hours * 3600, tz=timezone.utc)


def test_linear_triangle_windows():
    # Heights 0..12 h: rise to 10 m, fall to 0, then slight rise below
    # threshold. Safety threshold = draft 4 + clearance 1 = 5 m.
    times = [dt(h) for h in range(13)]
    heights = [0, 2, 4, 6, 8, 10, 8, 6, 4, 2, 0, 2, 4]

    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(12),
        draft_m=4.0,
        clearance_m=1.0,  # threshold = 5
        max_gap_seconds=4000,
    )
    assert res.threshold_m == 5.0
    assert res.unknown == []
    # Knots 3..7 (03:00..07:00, heights 6..10..6) form one continuous run;
    # the rising crossing is at 02:30 (9000 s), the falling at 07:30 (27000 s).
    assert len(res.windows) == 1
    w = res.windows[0]
    assert w.start == datetime.fromtimestamp(math.ceil(9000), tz=timezone.utc)
    assert w.end == datetime.fromtimestamp(math.floor(27000), tz=timezone.utc)
    # Every rounded boundary/inside knot satisfies the threshold.
    assert w.start_height_m >= 5.0 - 1e-9
    assert w.end_height_m >= 5.0 - 1e-9
    assert w.min_height_m >= 5.0 - 1e-9
    assert w.duration_seconds == 18000


def test_gap_splits_windows_and_is_unknown():
    # Two safe plateaus separated by a 3-hour gap (> max_gap).
    times = [dt(h) for h in (0, 1, 2, 5, 6, 7)]
    heights = [8, 9, 8, 8, 9, 8]
    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(7),
        draft_m=5.0,
        clearance_m=0.0,
        max_gap_seconds=3600 + 60,  # 3 h gap exceeds this
    )
    assert len(res.windows) == 2
    # Windows never span the forbidden gap.
    assert res.windows[0].end <= dt(2)
    assert res.windows[1].start >= dt(5)
    reasons = [u.reason for u in res.unknown]
    assert "sample_gap_exceeds_max_gap" in reasons
    gap = next(u for u in res.unknown if u.reason == "sample_gap_exceeds_max_gap")
    assert gap.start == dt(2) and gap.end == dt(5)


def test_query_outside_coverage_is_unknown():
    times = [dt(2), dt(3)]
    heights = [9.0, 9.0]
    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(10),
        draft_m=5.0,
        clearance_m=0.0,
        max_gap_seconds=7200,
    )
    assert len(res.windows) == 1
    assert res.windows[0].start == dt(2) and res.windows[0].end == dt(3)
    assert {u.reason for u in res.unknown} == {"no_sample_coverage"}
    assert res.unknown[0].start == dt(0)
    assert res.unknown[-1].end == dt(10)


def test_no_safe_window():
    times = [dt(0), dt(1), dt(2)]
    heights = [1.0, 1.2, 1.0]
    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=dt(0),
        query_end=dt(2),
        draft_m=5.0,
        clearance_m=0.0,
        max_gap_seconds=7200,
    )
    assert res.windows == []
    assert res.unknown == []


def test_crossing_rounded_to_seconds():
    # Crossing at 100.5 s after the query start: start rounds up.
    t0 = datetime(2026, 9, 21, tzinfo=timezone.utc)
    times = [
        t0,
        datetime.fromtimestamp(t0.timestamp() + 201, tz=timezone.utc),
    ]
    heights = [0.0, 20.0]  # threshold 10 crosses at 100.5 s
    res = compute_navigation_windows(
        sample_times=times,
        sample_heights=heights,
        query_start=t0,
        query_end=times[1],
        draft_m=10.0,
        clearance_m=0.0,
        max_gap_seconds=10000,
    )
    assert len(res.windows) == 1
    assert res.windows[0].start == datetime.fromtimestamp(
        t0.timestamp() + 101, tz=timezone.utc
    )
    assert res.windows[0].end == times[1]


def _plateau_payload(**kwargs):
    # Safe plateau 00:00..10:00 (height 9, threshold 5) with a 2-hour
    # forbidden gap between 04:00 and 06:00.
    times = [
        dt(h) for h in (0, 1, 2, 3, 4, 6, 7, 8, 9, 10)
    ]
    return compute_navigation_windows(
        sample_times=times,
        sample_heights=[9.0] * 10,
        query_start=dt(0),
        query_end=dt(10),
        draft_m=5.0,
        clearance_m=0.0,
        max_gap_seconds=3600 + 1,
        **kwargs,
    )


def test_departure_without_new_fields_is_empty():
    res = _plateau_payload()
    assert res.departure_windows == []


def test_departure_windows_fit_inside_safe_windows_and_never_cross_unknown():
    # One-hour transit: viable departures are [00:00, 03:00:01) on the
    # closed safe window [00:00, 04:00] (departing at 03:00 occupies
    # [03:00, 04:00), which reaches but does not pass the closed end) and
    # [06:00, 09:00:01) on [06:00, 10:00].
    res = _plateau_payload(transit_duration_seconds=3600)
    assert [
        (d.start, d.end) for d in res.departure_windows
    ] == [
        (dt(0), datetime.fromtimestamp(3 * 3600 + 1, tz=timezone.utc)),
        (dt(6), datetime.fromtimestamp(9 * 3600 + 1, tz=timezone.utc)),
    ]
    # End is half-open: last viable departure is one second before it.
    first = res.departure_windows[0]
    assert first.duration_seconds == 3 * 3600 + 1
    assert (first.end - first.start).total_seconds() == first.duration_seconds
    # No departure window bridges the 04:00..06:00 unknown stretch.
    assert all(d.end <= datetime.fromtimestamp(4 * 3600 + 1, tz=timezone.utc)
               or d.start >= dt(6)
               for d in res.departure_windows)


def test_departure_windows_blocked_by_shifted_maintenance():
    # Closure 02:00..02:30 shifted earlier by one hour forbids
    # departures in [01:00, 01:30), splitting the first candidate run.
    res = _plateau_payload(
        transit_duration_seconds=3600,
        maintenance_blocks=[(dt(2), dt(2.5))],
    )
    assert [
        (d.start, d.end) for d in res.departure_windows
    ] == [
        (dt(0), dt(1)),
        (dt(1.5), datetime.fromtimestamp(3 * 3600 + 1, tz=timezone.utc)),
        (dt(6), datetime.fromtimestamp(9 * 3600 + 1, tz=timezone.utc)),
    ]


def test_departure_maintenance_half_open_boundary_endpoint_allowed():
    # Occupancy [d, d+D) and closures are half-open.  Closure [01:00,02:00)
    # shifted earlier by one hour forbids departures in [00:00, 01:00):
    # departing at 01:00 (occupancy [02:00,03:00)) does not intersect it.
    res = _plateau_payload(
        transit_duration_seconds=3600,
        maintenance_blocks=[(dt(1), dt(2))],
    )
    first = res.departure_windows[0]
    assert first.start == dt(1) and first.end == datetime.fromtimestamp(
        3 * 3600 + 1, tz=timezone.utc
    )
    # Departure second 00:00 is removed and the run starts exactly at 01:00.
    assert all(d.start != dt(0) for d in res.departure_windows)


def test_departure_longer_than_window_yields_nothing():
    res = _plateau_payload(transit_duration_seconds=5 * 3600)
    assert res.departure_windows == []


def test_departure_zero_duration_returns_safe_windows_half_open():
    res = _plateau_payload(transit_duration_seconds=0.0)
    # Safe windows [00:00,04:00] and [06:00,10:00] become half-open
    # departure intervals [start, end+1s).
    assert [
        (d.start, d.end) for d in res.departure_windows
    ] == [
        (dt(0), datetime.fromtimestamp(4 * 3600 + 1, tz=timezone.utc)),
        (dt(6), datetime.fromtimestamp(10 * 3600 + 1, tz=timezone.utc)),
    ]


def test_maintenance_blocks_clipped_merged_and_sorted():
    blocks = _clip_merge_blocks(
        [
            (20.0, 30.0),
            (10.0, 20.0),   # abuts the previous one
            (15.0, 25.0),   # overlaps both
            (40.0, 50.0),
            (-10.0, 5.0),   # clipped to the query start
            (95.0, 120.0),  # clipped to the query end
            (200.0, 300.0),  # entirely outside -> dropped
            (50.0, 50.0),   # empty -> dropped
        ],
        0.0,
        100.0,
    )
    assert blocks == [(0.0, 5.0), (10.0, 30.0), (40.0, 50.0), (95.0, 100.0)]


def test_departure_blocks_outside_query_are_ignored():
    res = _plateau_payload(
        transit_duration_seconds=3600,
        maintenance_blocks=[(dt(20), dt(22))],
    )
    assert [
        (d.start, d.end) for d in res.departure_windows
    ] == [
        (dt(0), datetime.fromtimestamp(3 * 3600 + 1, tz=timezone.utc)),
        (dt(6), datetime.fromtimestamp(9 * 3600 + 1, tz=timezone.utc)),
    ]
