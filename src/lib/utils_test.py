from datetime import date

from src.lib.units import format_duration, format_pace
from src.lib.utils import compact_json, downsample, split_date_range


def test_split_date_range_covers_whole_range_without_overlap():
    # Act
    windows = split_date_range(date(2026, 1, 1), date(2026, 1, 10), max_days=4)

    # Assert
    assert windows == [
        (date(2026, 1, 1), date(2026, 1, 4)),
        (date(2026, 1, 5), date(2026, 1, 8)),
        (date(2026, 1, 9), date(2026, 1, 10)),
    ]


def test_downsample_keeps_first_and_last():
    # Act
    sample = downsample(list(range(101)), max_items=5)

    # Assert
    assert sample == [0, 25, 50, 75, 100]


def test_downsample_with_zero_returns_empty():
    # Act / Assert
    assert downsample([1, 2, 3], max_items=0) == []


def test_compact_json_removes_empty_values_recursively():
    # Act
    compacted = compact_json(
        {"a": None, "b": "", "c": [], "d": {"e": None}, "f": [1, None], "g": 0}
    )

    # Assert
    assert compacted == {"f": [1], "g": 0}


def test_format_pace_and_duration():
    # Act / Assert
    assert format_pace(3.333) == "5:00"
    assert format_pace(1.25, per_meters=100) == "1:20"
    assert format_pace(0) is None
    assert format_duration(3725) == "1:02:05"
