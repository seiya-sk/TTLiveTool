#!/usr/bin/env python3
"""系統2: 進捗通知の送信(毎時01分に直前1時間の窓を送る)。

二重送信防止は notification_digest_log の UNIQUE(group_id, window_start)
が担う。「このグループのこの時間帯」は本質的に一意なので、汎用のdedup
キーを別に持つ必要がない。この制約のおかげで、取りこぼした窓を後から
安全に追送できる(送信済みは弾かれる)。

status の扱い:
  sent / skipped_empty / skipped_quiet_hours -> 確定。再送しない。
  failed -> 一時的な失敗とみなし、--backfill-hours の範囲内なら再試行する
            (ON CONFLICT DO UPDATE で行を上書きする)。
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor import db
from tiktok_monitor.notify import chatwork, progress

logger = logging.getLogger("progress_notifier")

TERMINAL_STATUSES = ("sent", "skipped_empty", "skipped_quiet_hours")


def in_quiet_hours(window_start_utc: str, start_hour: int, end_hour: int) -> bool:
    """窓の開始時刻(JST)が通知時間帯の外なら True。
    start > end(例 22-6)の日跨ぎ指定にも対応する。"""
    h = datetime.fromisoformat(window_start_utc).astimezone(progress.JST).hour
    if start_hour <= end_hour:
        return not (start_hour <= h < end_hour)
    return not (h >= start_hour or h < end_hour)


def already_finalized(conn, group_id: int, window_start: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM notification_digest_log WHERE group_id = ? AND window_start = ?",
        (group_id, window_start),
    ).fetchone()
    return row[0] if row and row[0] in TERMINAL_STATUSES else None


def record(conn, group_id: int, window_start: str, window_end: str, status: str, detail: str) -> None:
    conn.execute(
        """
        INSERT INTO notification_digest_log (group_id,window_start,window_end,status,detail,sent_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(group_id, window_start) DO UPDATE SET
            status = excluded.status, detail = excluded.detail, sent_at = excluded.sent_at
        """,
        (group_id, window_start, window_end, status, detail[:1000], db.utc_now_iso()),
    )
    conn.commit()


def load_groups(conn, group_id: int | None) -> list[dict]:
    sql = ("SELECT id,name,room_id,to_account_ids,enabled,send_when_empty,"
           "notify_start_hour,notify_end_hour FROM notification_groups")
    params: tuple = ()
    if group_id:
        sql += " WHERE id = ?"
        params = (group_id,)
    return [
        {"id": r[0], "name": r[1], "room_id": r[2],
         "to": json.loads(r[3]) if r[3] else [], "enabled": bool(r[4]),
         "send_when_empty": bool(r[5]), "start_hour": r[6], "end_hour": r[7]}
        for r in conn.execute(sql, params)
    ]


def member_ids(conn, group_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT streamer_id FROM notification_group_streamers WHERE group_id = ?", (group_id,))]


def windows_to_process(backfill_hours: int) -> list[tuple[str, str]]:
    """新しい窓から順に backfill_hours 個。既に確定済みの窓は呼び出し側で弾く。"""
    start, end = progress.hour_window()
    out = []
    for i in range(max(1, backfill_hours)):
        delta = timedelta(hours=i)
        out.append((
            (datetime.fromisoformat(start) - delta).isoformat(),
            (datetime.fromisoformat(end) - delta).isoformat(),
        ))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", required=True)
    p.add_argument("--group-id", type=int, help="このグループだけを対象にする")
    p.add_argument("--backfill-hours", type=int, default=3,
                   help="さかのぼって未送信の窓を補完する時間数(既定3)")
    p.add_argument("--dry-run", action="store_true",
                   help="送信せず内容を表示。digest_log にも一切書き込まない "
                        "(書き込むと窓が確定扱いになり、後の本番送信が抑止されてしまうため)")
    p.add_argument("--test-send", action="store_true",
                   help="時間帯・二重送信チェックを無視して直近の窓を1通送る(UIのテスト送信用)。"
                        "digest_log には記録しない")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    conn = db.connect(args.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        groups = load_groups(conn, args.group_id)
        if not groups:
            logger.info("対象グループがありません")
            return 0

        windows = windows_to_process(1 if args.test_send else args.backfill_hours)
        sent = skipped = failed = 0

        for g in groups:
            if not g["enabled"] and not args.test_send:
                continue
            ids = member_ids(conn, g["id"])

            for window_start, window_end in windows:
                if not args.test_send:
                    done = already_finalized(conn, g["id"], window_start)
                    if done:
                        continue
                    if in_quiet_hours(window_start, g["start_hour"], g["end_hour"]):
                        if not args.dry_run:
                            record(conn, g["id"], window_start, window_end, "skipped_quiet_hours",
                                   f"通知時間帯 {g['start_hour']}-{g['end_hour']} 外")
                        skipped += 1
                        continue

                result = progress.collect_group_progress(conn, ids, window_start, window_end)

                if not args.test_send and result["session_count"] == 0 and not g["send_when_empty"]:
                    if not args.dry_run:
                        record(conn, g["id"], window_start, window_end, "skipped_empty", "配信なし")
                    skipped += 1
                    continue

                title = g["name"] + ("(テスト送信)" if args.test_send else "")
                message = progress.format_digest(title, result, window_start, window_end)
                detail = (f"{result['streamer_count']}人/{result['session_count']}本/"
                          f"{progress.format_duration(result['total_seconds'])}/"
                          f"💎{result['total_diamonds']}")

                if args.dry_run:
                    print(f"\n===== [dry-run] group={g['name']} "
                          f"{progress.window_label(window_start, window_end)} =====\n{message}\n")
                    sent += 1
                    continue

                try:
                    chatwork.send_message(g["room_id"], message, g["to"])
                    logger.info("送信: group=%s window=%s (%s)", g["name"],
                                progress.window_label(window_start, window_end), detail)
                    sent += 1
                    if not args.test_send:
                        record(conn, g["id"], window_start, window_end, "sent", detail)
                except chatwork.ChatworkError as exc:
                    logger.error("送信失敗: group=%s window=%s: %s", g["name"], window_start, exc)
                    failed += 1
                    if not args.test_send:
                        # failed は確定扱いにしない -- backfill 範囲内で再試行される
                        record(conn, g["id"], window_start, window_end, "failed", str(exc))

        logger.info("%s送信 %d / スキップ %d / 失敗 %d",
                    "[dry-run] " if args.dry_run else "", sent, skipped, failed)
        return 1 if failed and not sent else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
