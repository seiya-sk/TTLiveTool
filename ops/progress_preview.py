#!/usr/bin/env python3
"""ステップ4検証用: 進捗集計の数値を、送信せず標準出力に出す。

グループを作る前でも数値を検証できるよう --all で全ライバーを1つの
擬似グループとして扱える。--group-id で実グループも指定できる。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor import db
from tiktok_monitor.notify import progress


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", required=True)
    p.add_argument("--all", action="store_true", help="全ライバー(archived除く)を擬似グループとして集計")
    p.add_argument("--group-id", type=int)
    p.add_argument("--window-start", help="UTC ISO。省略時は直前の1時間")
    p.add_argument("--window-end", help="UTC ISO")
    p.add_argument("--json", action="store_true", help="生の数値をJSONで出す")
    args = p.parse_args()

    conn = db.connect(args.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")

    if args.window_start and args.window_end:
        start, end = args.window_start, args.window_end
    else:
        start, end = progress.hour_window()

    if args.all:
        name = "(全ライバー・擬似グループ)"
        ids = [r[0] for r in conn.execute("SELECT id FROM streamers WHERE archived = 0 ORDER BY id")]
    elif args.group_id:
        row = conn.execute("SELECT name FROM notification_groups WHERE id = ?", (args.group_id,)).fetchone()
        if not row:
            print(f"group_id={args.group_id} が存在しません", file=sys.stderr)
            return 1
        name = row[0]
        ids = [r[0] for r in conn.execute(
            "SELECT streamer_id FROM notification_group_streamers WHERE group_id = ?", (args.group_id,))]
    else:
        print("--all か --group-id のどちらかを指定してください", file=sys.stderr)
        return 1

    result = progress.collect_group_progress(conn, ids, start, end)
    conn.close()

    if args.json:
        print(json.dumps({"window": [start, end], "group": name, **result}, ensure_ascii=False, indent=2))
    else:
        print(f"窓: {start} 〜 {end}  ({progress.window_label(start, end)} JST)")
        print(f"対象ライバー: {len(ids)}人\n")
        print(progress.format_digest(name, result, start, end))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
