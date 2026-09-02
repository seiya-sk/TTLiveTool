"""Analyzes data/concurrent_live.jsonl (see watch.py's concurrency_poll_loop)
to answer the actual business question the measurement exists for: how many
watched streamers are live at once, and when does that peak, so real-ops
IP-pool sizing can be based on measured data instead of a guess.

All aggregation is in JST (Asia/Tokyo) -- concurrent_live.jsonl stores UTC
timestamps (Python's datetime.isoformat() output), matching every other
timestamp in this project, but "何時が一番混むか" is a JST question.

Run: python -m tiktok_monitor.analyze_concurrency [--path data/concurrent_live.jsonl]
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .watch import CHECK_PACE_SEC, DEFAULT_CONCURRENCY_LOG_PATH

# Windows consoles often default stdout to a non-UTF-8 codepage (e.g.
# cp932), which garbles this script's Japanese output even though the
# strings themselves are correct -- force UTF-8 regardless of the host
# console's active codepage. reconfigure() is a no-op-safe TextIOWrapper
# method (Python 3.7+); guarded because a redirected/piped stdout without
# a .reconfigure (rare, but e.g. some IDE-embedded consoles) shouldn't
# crash the whole report.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

JST = timezone(timedelta(hours=9))

# A gap between two consecutive sweeps is flagged as "possible downtime" --
# not just a naturally slow sweep of a large population -- only when it's
# BOTH past a generous floor AND well past what that sweep's own
# checked_count would need at the safe per-check pace (see watch.py's
# CheckPacer/CHECK_PACE_SEC). A 528-streamer sweep alone can legitimately
# take ~44 minutes between two consecutive jsonl lines; a flat time
# threshold would misreport every one of those as a gap.
GAP_FLOOR_SEC = 600.0  # 10 minutes
GAP_MULTIPLIER = 2.5


def load_records(path: str) -> list[dict]:
    """Each dict gets dt_utc/dt_jst datetime objects added (aware, JST via
    dt_utc.astimezone) alongside the raw jsonl fields. Sorted by time --
    concurrent_live.jsonl is append-only in order, but sorting makes this
    robust to a manually concatenated/edited file too."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["dt_utc"] = datetime.fromisoformat(row["timestamp"])
            row["dt_jst"] = row["dt_utc"].astimezone(JST)
            records.append(row)
    records.sort(key=lambda r: r["dt_utc"])
    return records


def find_gaps(records: list[dict]) -> list[dict]:
    gaps = []
    for prev, cur in zip(records, records[1:]):
        gap_sec = (cur["dt_utc"] - prev["dt_utc"]).total_seconds()
        expected_sec = prev["checked_count"] * CHECK_PACE_SEC
        if gap_sec > max(GAP_FLOOR_SEC, expected_sec * GAP_MULTIPLIER):
            gaps.append(
                {
                    "from": prev["dt_jst"],
                    "to": cur["dt_jst"],
                    "gap_minutes": gap_sec / 60.0,
                    "checked_count_before": prev["checked_count"],
                }
            )
    return gaps


def per_day_peaks(records: list[dict]) -> dict[date, dict]:
    """One entry per JST calendar date -> the single record with that day's
    highest live_count (first occurrence wins on a tie)."""
    by_day: dict[date, list[dict]] = defaultdict(list)
    for r in records:
        by_day[r["dt_jst"].date()].append(r)
    return {day: max(rows, key=lambda r: r["live_count"]) for day, rows in by_day.items()}


def per_hour_stats(records: list[dict]) -> dict[int, dict]:
    """JST hour-of-day (0-23) -> {max, avg, n} across every record from any
    day that fell in that hour."""
    by_hour: dict[int, list[int]] = defaultdict(list)
    for r in records:
        by_hour[r["dt_jst"].hour].append(r["live_count"])
    return {
        hour: {"max": max(counts), "avg": sum(counts) / len(counts), "n": len(counts)}
        for hour, counts in by_hour.items()
    }


def most_recent_complete_day(peaks: dict[date, dict]) -> date | None:
    """"昨夜" = the latest JST calendar date in the data that isn't the
    same date as the very last record (which may still be an in-progress,
    partial day)."""
    if not peaks:
        return None
    days = sorted(peaks)
    latest_day = days[-1]
    for day in reversed(days):
        if day < latest_day:
            return day
    return None  # only one day of data exists so far


def _fmt_usernames(usernames: list[str]) -> str:
    return ", ".join(usernames) if usernames else "(なし)"


def print_report(records: list[dict]) -> None:
    if not records:
        print("レコードがありません。")
        return

    first, last = records[0], records[-1]
    checked_counts_seen = sorted({r["checked_count"] for r in records})

    print("=" * 70)
    print("concurrent_live.jsonl 集計 (JST)")
    print("=" * 70)
    print(f"レコード数: {len(records)}")
    print(f"測定期間: {first['dt_jst']:%Y-%m-%d %H:%M} 〜 {last['dt_jst']:%Y-%m-%d %H:%M} (JST)")
    print(f"観測対象人数の推移(checked_count): {checked_counts_seen}")
    print()

    peaks = per_day_peaks(records)
    print("★★★ 昨夜のピーク ★★★")
    last_night = most_recent_complete_day(peaks)
    if last_night:
        row = peaks[last_night]
        print(f"{last_night}(直近の完了日): {row['live_count']}/{row['checked_count']} 本  @ {row['dt_jst']:%H:%M:%S} (JST)")
        print(f"内訳: {_fmt_usernames(row['live_usernames'])}")
    else:
        print("前日分のデータがまだありません(1日分たまっていない可能性があります)。")
    print()

    overall_peak = max(records, key=lambda r: r["live_count"])
    print("--- 全期間の最大同時配信数 ---")
    print(
        f"{overall_peak['live_count']}/{overall_peak['checked_count']} 本  "
        f"@ {overall_peak['dt_jst']:%Y-%m-%d %H:%M:%S} (JST)"
    )
    print(f"内訳: {_fmt_usernames(overall_peak['live_usernames'])}")
    print()

    print("--- 日ごとのピーク同時配信数 ---")
    for day in sorted(peaks):
        row = peaks[day]
        print(f"{day}: {row['live_count']}/{row['checked_count']} 本  @ {row['dt_jst']:%H:%M:%S}")
    print()

    hour_stats = per_hour_stats(records)
    print("--- 時間帯ごとの最大・平均同時配信数 (JST, 全期間) ---")
    print(f"{'時':>4} | {'最大':>6} | {'平均':>7} | {'サンプル数':>8}")
    for hour in range(24):
        s = hour_stats.get(hour)
        if s:
            print(f"{hour:>4} | {s['max']:>6} | {s['avg']:>7.2f} | {s['n']:>8}")
    print()

    gaps = find_gaps(records)
    print(f"--- データの欠落(空白期間の疑い): {len(gaps)}件 ---")
    if not gaps:
        print("(検出なし)")
    for g in gaps:
        print(
            f"{g['from']:%Y-%m-%d %H:%M} 〜 {g['to']:%Y-%m-%d %H:%M} (JST)  "
            f"約{g['gap_minutes']:.0f}分  (直前の観測対象人数: {g['checked_count_before']})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze concurrent_live.jsonl for peak concurrency (JST).")
    parser.add_argument("--path", default=DEFAULT_CONCURRENCY_LOG_PATH)
    args = parser.parse_args()

    records = load_records(args.path)
    print_report(records)


if __name__ == "__main__":
    main()
