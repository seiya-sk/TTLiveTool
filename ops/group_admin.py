#!/usr/bin/env python3
"""進捗通知グループの管理CLI。

ステップ6のUIができれば通常はそちらから操作するが、UIが無くても
運用・障害対応でグループを確認/修正できるようにしておく。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor import db


def cmd_list(conn, args):
    groups = conn.execute(
        "SELECT id,name,room_id,to_account_ids,enabled,send_when_empty,"
        "notify_start_hour,notify_end_hour FROM notification_groups ORDER BY id"
    ).fetchall()
    if not groups:
        print("グループはまだありません。")
        return 0
    for g in groups:
        members = conn.execute(
            "SELECT st.name FROM notification_group_streamers gs "
            "JOIN streamers st ON st.id = gs.streamer_id WHERE gs.group_id = ? ORDER BY st.name",
            (g[0],),
        ).fetchall()
        to = json.loads(g[3]) if g[3] else []
        print(f"[{g[0]}] {g[1]}")
        print(f"     room={g[2]}  to={to or '(なし)'}  enabled={bool(g[4])}  "
              f"send_when_empty={bool(g[5])}  通知時間帯={g[6]}:00-{g[7]}:00 JST")
        print(f"     ライバー {len(members)}人: {', '.join(m[0] for m in members) or '(未割り当て)'}")
    return 0


def cmd_create(conn, args):
    now = db.utc_now_iso()
    cur = conn.execute(
        "INSERT INTO notification_groups (name,room_id,to_account_ids,enabled,send_when_empty,"
        "notify_start_hour,notify_end_hour,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (args.name, args.room_id, json.dumps(args.to or []), 1, 0,
         args.start_hour, args.end_hour, now, now),
    )
    conn.commit()
    print(f"グループを作成しました: id={cur.lastrowid} name={args.name}")
    return 0


def cmd_assign(conn, args):
    if args.all_active:
        ids = [r[0] for r in conn.execute("SELECT id FROM streamers WHERE archived = 0 ORDER BY id")]
    else:
        ids = []
        for name in args.streamer:
            row = conn.execute(
                "SELECT id FROM streamers WHERE tiktok_account_id = ? OR name = ?", (name, name)
            ).fetchone()
            if not row:
                print(f"  警告: ライバーが見つかりません: {name}", file=sys.stderr)
                continue
            ids.append(row[0])
    now = db.utc_now_iso()
    if args.replace:
        conn.execute("DELETE FROM notification_group_streamers WHERE group_id = ?", (args.group_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO notification_group_streamers (group_id,streamer_id,created_at) VALUES (?,?,?)",
        [(args.group_id, sid, now) for sid in ids],
    )
    conn.commit()
    print(f"group_id={args.group_id} に {len(ids)} 人を割り当てました")
    return 0


def cmd_unassigned(conn, args):
    rows = conn.execute(
        "SELECT id, name FROM streamers WHERE archived = 0 AND id NOT IN "
        "(SELECT streamer_id FROM notification_group_streamers) ORDER BY name"
    ).fetchall()
    print(f"未割り当てライバー: {len(rows)}人")
    for r in rows:
        print(f"  [{r[0]}] {r[1]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("unassigned").set_defaults(func=cmd_unassigned)

    c = sub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--room-id", required=True)
    c.add_argument("--to", nargs="*", help="Chatwork account_id(複数可)")
    c.add_argument("--start-hour", type=int, default=9)
    c.add_argument("--end-hour", type=int, default=24)
    c.set_defaults(func=cmd_create)

    a = sub.add_parser("assign")
    a.add_argument("--group-id", type=int, required=True)
    a.add_argument("--streamer", nargs="*", default=[])
    a.add_argument("--all-active", action="store_true", help="archived でない全ライバー")
    a.add_argument("--replace", action="store_true", help="既存の割り当てを置き換える")
    a.set_defaults(func=cmd_assign)

    args = p.parse_args()
    conn = db.connect(args.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
