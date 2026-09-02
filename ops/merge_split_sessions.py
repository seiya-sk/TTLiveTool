#!/usr/bin/env python3
"""同一 room_id に分かれてしまったセッションを1本に統合する。

旧ロジック(idle timeout による誤終了)と、収集プロセスの再起動
(end_detection_type='manual' が再開対象外だった)で、1本の配信が複数の
live_sessions に割れていた。新ロジックで新規分割は止まったので、既存の
分を掃除する。

安全策(cleanup_job.py と同じ):
  - busy_timeout 30秒。競合したら「録画を待たせる」のではなく「統合が待つ」
  - グループ単位の短いトランザクション。全体を1つの巨大トランザクションに
    しないことで、録画側の書き込みがロック待ちで詰まる時間を最小にする
  - **進行中(status='live')のセッションを含むグループは触らない**。
    録画プロセスが書き込んでいる最中に live_session_id を付け替えると競合する

統合の中身:
  代表 = そのグループで最も started_at が早いセッション
  live_events / live_screenshots / live_reports の live_session_id を代表へ付け替え
  (live_event_raw_payloads は live_event_id 参照なので自動的に追従する)
  代表の started_at = 最早、ended_at = 最遅、end_detection_type = 最後に
  終わったセッションのもの(= その配信が実際にどう終わったか)
  余ったセッション行は削除
"""
import argparse
import collections
import json
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor import db

logger = logging.getLogger("merge_split_sessions")

FORENSICS = [
    "data/forensics/session_room_ids.json",
    "data/forensics/session_room_ids_16_35.json",
]


def load_forensic_room_ids(base_dir: str) -> dict[int, str]:
    """room_id 列が無かった時期のセッションの room_id を、生ペイロードから
    抽出して保全しておいたファイルから読む。retention で生ペイロードが
    消えた後でも統合できるようにするための備え。"""
    out: dict[int, str] = {}
    for rel in FORENSICS:
        path = os.path.join(base_dir, rel)
        if not os.path.exists(path):
            continue
        for entry in json.load(open(path, encoding="utf-8")):
            sid = entry["session_id"]
            room = entry.get("room_id")
            if room is None and entry.get("room_ids"):
                room = entry["room_ids"][0]["room_id"]
            if room:
                out.setdefault(sid, str(room))
    return out


def build_groups(conn: sqlite3.Connection, forensic: dict[int, str]):
    rows = conn.execute(
        "SELECT s.id, st.name, s.streamer_id, s.started_at, s.ended_at, s.status, "
        "s.end_detection_type, s.room_id, "
        "(SELECT COUNT(*) FROM live_events WHERE live_session_id = s.id) "
        "FROM live_sessions s JOIN streamers st ON st.id = s.streamer_id ORDER BY s.id"
    ).fetchall()

    groups = collections.defaultdict(list)
    unknown = []
    for sid, name, streamer, started, ended, status, etype, room, events in rows:
        resolved = room or forensic.get(sid)
        if not resolved:
            unknown.append(sid)
            continue
        groups[(streamer, name, resolved)].append(
            dict(id=sid, started=started, ended=ended, status=status, etype=etype, events=events)
        )
    return groups, unknown, len(rows)


def merge_group(conn: sqlite3.Connection, room_id: str, sessions: list[dict], dry_run: bool) -> dict:
    sessions.sort(key=lambda s: s["started"])
    rep = sessions[0]["id"]
    others = [s["id"] for s in sessions[1:]]
    started = sessions[0]["started"]
    ended = max(s["ended"] for s in sessions)
    # その配信が「実際にどう終わったか」は、最後に終わったセッションが持つ
    last = max(sessions, key=lambda s: s["ended"])
    result = {
        "representative": rep, "removed": others, "room_id": room_id,
        "started_at": started, "ended_at": ended,
        "end_detection_type": last["etype"],
        "events": sum(s["events"] for s in sessions),
    }
    if dry_run:
        return result

    placeholders = ",".join("?" * len(others))
    # グループ1つで1トランザクション。BEGIN IMMEDIATE で書き込みロックを
    # 最初に取り、途中で他の書き込みに割り込まれて中途半端に終わるのを防ぐ。
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in ("live_events", "live_screenshots", "live_reports"):
            conn.execute(
                f"UPDATE {table} SET live_session_id = ? WHERE live_session_id IN ({placeholders})",
                (rep, *others),
            )
        conn.execute(
            "UPDATE live_sessions SET started_at = ?, ended_at = ?, status = 'ended', "
            "end_detection_type = ?, room_id = ? WHERE id = ?",
            (started, ended, last["etype"], room_id, rep),
        )
        conn.execute(f"DELETE FROM live_sessions WHERE id IN ({placeholders})", others)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--base-dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    forensic = load_forensic_room_ids(args.base_dir)
    conn = db.connect(args.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.isolation_level = None  # 明示的な BEGIN/COMMIT を使う

    before_sessions = conn.execute("SELECT COUNT(*) FROM live_sessions").fetchone()[0]
    before_events = conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0]
    before_shots = conn.execute("SELECT COUNT(*) FROM live_screenshots").fetchone()[0]

    groups, unknown, total = build_groups(conn, forensic)
    if unknown:
        logger.warning("room_id が特定できないセッション(対象外): %s", unknown)

    merged, deferred = [], []
    for (_streamer, name, room), sessions in sorted(groups.items(), key=lambda g: g[0][1]):
        if len(sessions) < 2:
            continue
        if any(s["status"] == "live" for s in sessions):
            deferred.append((name, room, [s["id"] for s in sessions]))
            continue
        info = merge_group(conn, room, sessions, args.dry_run)
        merged.append((name, info))
        logger.info(
            "%s%s: id=%s に統合 (削除 %s) %s→%s events=%d",
            "[dry-run] " if args.dry_run else "", name,
            info["representative"], info["removed"],
            info["started_at"][:19], info["ended_at"][:19], info["events"],
        )

    after_sessions = conn.execute("SELECT COUNT(*) FROM live_sessions").fetchone()[0]
    after_events = conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0]
    after_shots = conn.execute("SELECT COUNT(*) FROM live_screenshots").fetchone()[0]

    dups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT streamer_id, room_id FROM live_sessions "
        "WHERE room_id IS NOT NULL GROUP BY streamer_id, room_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    orphan_events = conn.execute(
        "SELECT COUNT(*) FROM live_events e LEFT JOIN live_sessions s ON s.id = e.live_session_id "
        "WHERE s.id IS NULL"
    ).fetchone()[0]

    print()
    print(f"  セッション : {before_sessions} → {after_sessions}  (統合 {len(merged)}グループ)")
    print(f"  イベント   : {before_events} → {after_events}  "
          f"{'✓ 不変' if before_events == after_events else '★ 増減あり'}")
    print(f"  スクショ   : {before_shots} → {after_shots}  "
          f"{'✓ 不変' if before_shots == after_shots else '★ 増減あり'}")
    print(f"  孤児イベント(存在しないセッションを指す): {orphan_events}  "
          f"{'✓' if orphan_events == 0 else '★'}")
    print(f"  room_id 重複: {dups}件  {'✓' if dups == len(deferred) else '★ 想定外'}"
          f"  (うち進行中で見送り {len(deferred)}件)")
    if deferred:
        print("\n  見送ったグループ(録画中。そのライブが終わってから統合):")
        for name, room, ids in deferred:
            print(f"    {name:20} room={room} sessions={ids}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
