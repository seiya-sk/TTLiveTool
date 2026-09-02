import json
import sys
from datetime import date, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.analyze_concurrency import (
    find_gaps,
    load_records,
    most_recent_complete_day,
    per_day_peaks,
    per_hour_stats,
)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def make_row(timestamp: str, checked_count: int, live_count: int, live_usernames: list[str] | None = None) -> dict:
    return {
        "timestamp": timestamp,
        "checked_count": checked_count,
        "live_count": live_count,
        "live_usernames": live_usernames or [],
    }


def test_load_records_parses_utc_timestamp_and_converts_to_jst(tmp_path):
    path = tmp_path / "concurrent_live.jsonl"
    # 2026-08-29T15:00:00+00:00 UTC == 2026-08-30T00:00:00+09:00 JST
    write_jsonl(path, [make_row("2026-08-29T15:00:00+00:00", 5, 2, ["a", "b"])])

    records = load_records(str(path))

    assert len(records) == 1
    r = records[0]
    assert r["dt_utc"].tzinfo == timezone.utc
    assert r["dt_jst"].strftime("%Y-%m-%d %H:%M:%S") == "2026-08-30 00:00:00"
    assert r["live_count"] == 2
    assert r["live_usernames"] == ["a", "b"]


def test_load_records_skips_blank_lines_and_sorts_by_time(tmp_path):
    path = tmp_path / "concurrent_live.jsonl"
    path.write_text(
        json.dumps(make_row("2026-08-29T10:00:00+00:00", 5, 1)) + "\n"
        "\n"
        + json.dumps(make_row("2026-08-29T09:00:00+00:00", 5, 0)) + "\n",
        encoding="utf-8",
    )

    records = load_records(str(path))

    assert len(records) == 2
    assert records[0]["timestamp"] == "2026-08-29T09:00:00+00:00"  # earlier record sorted first
    assert records[1]["timestamp"] == "2026-08-29T10:00:00+00:00"


def test_per_day_peaks_picks_the_max_live_count_row_per_jst_calendar_day(tmp_path):
    path = tmp_path / "concurrent_live.jsonl"
    write_jsonl(
        path,
        [
            make_row("2026-08-29T00:00:00+00:00", 5, 1),  # 2026-08-29 09:00 JST
            make_row("2026-08-29T10:00:00+00:00", 5, 4),  # 2026-08-29 19:00 JST -- day's peak
            make_row("2026-08-29T20:00:00+00:00", 5, 2),  # 2026-08-30 05:00 JST -- next JST day
        ],
    )
    records = load_records(str(path))

    peaks = per_day_peaks(records)

    assert set(peaks) == {date(2026, 8, 29), date(2026, 8, 30)}
    assert peaks[date(2026, 8, 29)]["live_count"] == 4
    assert peaks[date(2026, 8, 30)]["live_count"] == 2


def test_most_recent_complete_day_excludes_the_last_records_own_date(tmp_path):
    path = tmp_path / "concurrent_live.jsonl"
    write_jsonl(
        path,
        [
            make_row("2026-08-28T15:00:00+00:00", 5, 3),  # 2026-08-29 JST
            make_row("2026-08-29T15:00:00+00:00", 5, 5),  # 2026-08-30 JST -- latest day, still "in progress"
        ],
    )
    records = load_records(str(path))
    peaks = per_day_peaks(records)

    assert most_recent_complete_day(peaks) == date(2026, 8, 29)


def test_most_recent_complete_day_returns_none_with_only_one_day_of_data(tmp_path):
    path = tmp_path / "concurrent_live.jsonl"
    write_jsonl(path, [make_row("2026-08-29T15:00:00+00:00", 5, 3)])
    records = load_records(str(path))
    peaks = per_day_peaks(records)

    assert most_recent_complete_day(peaks) is None


def test_per_hour_stats_aggregates_max_and_average_across_days_by_jst_hour(tmp_path):
    path = tmp_path / "concurrent_live.jsonl"
    write_jsonl(
        path,
        [
            make_row("2026-08-28T14:00:00+00:00", 5, 2),  # 2026-08-28 23:00 JST
            make_row("2026-08-29T14:00:00+00:00", 5, 6),  # 2026-08-29 23:00 JST -- same hour, different day
        ],
    )
    records = load_records(str(path))

    stats = per_hour_stats(records)

    assert stats[23] == {"max": 6, "avg": 4.0, "n": 2}


def test_find_gaps_flags_a_gap_far_beyond_the_expected_sweep_time(tmp_path):
    # checked_count=5 at CHECK_PACE_SEC=5.0 -> expected sweep ~25s; a
    # 46-minute gap is nowhere close to explainable by sweep time alone.
    path = tmp_path / "concurrent_live.jsonl"
    write_jsonl(
        path,
        [
            make_row("2026-08-29T05:21:00+00:00", 5, 0),
            make_row("2026-08-29T06:07:00+00:00", 5, 0),
        ],
    )
    records = load_records(str(path))

    gaps = find_gaps(records)

    assert len(gaps) == 1
    assert gaps[0]["checked_count_before"] == 5
    assert 45 < gaps[0]["gap_minutes"] < 47


def test_find_gaps_does_not_flag_a_normal_large_population_sweep(tmp_path):
    # 528 * 5.0s ~= 2640s (~44 min) is a NORMAL single-sweep duration at
    # that scale, not downtime -- must not be flagged.
    path = tmp_path / "concurrent_live.jsonl"
    write_jsonl(
        path,
        [
            make_row("2026-08-29T12:00:00+00:00", 528, 3),
            make_row("2026-08-29T12:44:00+00:00", 528, 5),  # 44 min later
        ],
    )
    records = load_records(str(path))

    gaps = find_gaps(records)

    assert gaps == []


def test_find_gaps_returns_empty_for_no_gaps(tmp_path):
    path = tmp_path / "concurrent_live.jsonl"
    write_jsonl(
        path,
        [
            make_row("2026-08-29T05:00:00+00:00", 5, 0),
            make_row("2026-08-29T05:01:00+00:00", 5, 0),
        ],
    )
    records = load_records(str(path))

    assert find_gaps(records) == []
