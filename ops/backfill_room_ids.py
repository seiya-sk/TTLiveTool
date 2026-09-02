#!/usr/bin/env python3
"""room_id 列が無かった時期のセッションに、保全しておいた room_id を埋める。

room_id 列は 2026-09-01 に追加した。それ以前に記録されたセッションは
構造的に NULL のままで、同一ライブ判定(分割の検出・統合)ができない。
生ペイロードが retention で消える前に抽出しておいた値
(data/forensics/session_room_ids*.json)から埋め戻す。

安全策(cleanup_job.py と同じ):
  - busy_timeout 30秒、短いトランザクション、録画プロセスは止めない
  - 既に room_id が入っている行は触らない(上書きしない)
  - 埋めることで新たな room_id 重複が生じる場合は、埋める前に報告する
    (重複は統合が必要という意味で、黙って作ってはいけない)
"""
import argparse
import collections
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor import db

logger = logging.getLogger("backfill_room_ids")
FORENSICS = ("session_room_ids.json", "session_room_ids_16_35.json")


def load_forensic(base_dir: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for name in FORENSICS:
        path = os.path.join(base_dir, "data", "forensics", name)
        if not os.path.exists(path):
            continue
        for entry in json.load(open(path, encoding="utf-8")):
            room = entry.get("room_id")
            if room is None and entry.get("room_ids"):
                room = entry["room_ids"][0]["room_id"]
            if room:
                out.setdefault(entry["session_id"], str(room))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", required=True)
    p.add_argument("--base-dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    forensic = load_forensic(args.base_dir)
    conn = db.connect(args.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")

    missing = conn.execute(
        "SELECT id, streamer_id FROM live_sessions WHERE room_id IS NULL ORDER BY id"
    ).fetchall()
    fillable = {sid: forensic[sid] for sid, _ in missing if sid in forensic}
    streamer_of = {sid: streamer for sid, streamer in missing}

    # 埋めることで重複が生じないか、先に確認する
    groups = collections.defaultdict(list)
    for sid, streamer, room in conn.execute(
        "SELECT id, streamer_id, room_id FROM live_sessions WHERE room_id IS NOT NULL"
    ):
        groups[(streamer, room)].append(sid)
    for sid, room in fillable.items():
        groups[(streamer_of[sid], room)].append(sid)
    new_dups = {k: v for k, v in groups.items() if len(v) > 1}
    if new_dups:
        logger.warning("埋めると room_id が重複するグループがあります(統合が必要):")
        for (streamer, room), ids in new_dups.items():
            logger.warning("  streamer=%s room=%s sessions=%s", streamer, room, sorted(ids))

    logger.info("%s未記録 %d件 / 埋められる %d件",
                "[dry-run] " if args.dry_run else "", len(missing), len(fillable))
    if not args.dry_run and fillable:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sid, room in fillable.items():
                # room_id IS NULL 条件を残す -- 実行中に録画側が値を入れた場合に
                # 上書きしないため(この処理は稼働中DBに対して走る)
                conn.execute(
                    "UPDATE live_sessions SET room_id = ? WHERE id = ? AND room_id IS NULL",
                    (room, sid),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        logger.info("%d件に room_id を埋めました", len(fillable))

    left = conn.execute("SELECT COUNT(*) FROM live_sessions WHERE room_id IS NULL").fetchone()[0]
    dups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT streamer_id, room_id FROM live_sessions "
        "WHERE room_id IS NOT NULL GROUP BY 1,2 HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    print(f"\n  room_id 未記録: {left}件  {'✓' if left == 0 else ''}")
    print(f"  room_id 重複  : {dups}件  {'✓' if dups == 0 else '← 統合が必要'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
