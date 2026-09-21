import math
from datetime import datetime, timezone

from app.services.tides import compute_navigation_windows


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
