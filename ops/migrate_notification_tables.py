#!/usr/bin/env python3
"""稼働中の録画DBへ、進捗通知(系統2)の3テーブルを追加する一度きりの移行。

正攻法は db.py の SCHEMA に追記して init_schema() を走らせることだが、
それが効くのは録画プロセスの次回起動時であって、長期テスト中の今は
再起動しない。そこで同じDDLだけを切り出して適用する。db.py 側にも同じ
定義を追記済みなので、将来の再起動時に整合が取れる(CREATE TABLE
IF NOT EXISTS なので二重適用も無害)。

安全策は cleanup_job.py と同じ:
  - busy_timeout を 30 秒に延ばし、競合時は「録画を待たせる」のではなく
    「移行が待つ」向きに倒す
  - CREATE TABLE IF NOT EXISTS のみで、既存データには一切触れない
  - 適用は数ミリ秒で終わるためロック保持時間が極端に短い
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor import db

logger = logging.getLogger("migrate_notification_tables")

TARGET_OBJECTS = [
    "notification_groups",
    "notification_group_streamers",
    "notification_digest_log",
    "idx_notification_group_streamers_streamer",
    "idx_notification_digest_log_group",
]

# db.py の SCHEMA から該当部分だけを切り出す。定義を二重管理しないよう、
# 文字列を複製するのではなく SCHEMA から抽出する。
def extract_ddl() -> str:
    marker = "-- 進捗通知(系統2)のグループ"
    idx = db.SCHEMA.find(marker)
    if idx == -1:
        raise SystemExit("db.py の SCHEMA に通知テーブルの定義が見つかりません")
    return db.SCHEMA[idx:]


def existing(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN ({})".format(
            ",".join("?" * len(TARGET_OBJECTS))
        ),
        TARGET_OBJECTS,
    ).fetchall()
    return {r[0] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ddl = extract_ddl()
    conn = db.connect(args.db_path)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        before = existing(conn)
        logger.info("適用前に存在するオブジェクト: %s", sorted(before) or "なし")
        missing = [o for o in TARGET_OBJECTS if o not in before]
        if not missing:
            logger.info("すべて適用済みです。何もしません。")
            return 0
        logger.info("作成対象: %s", missing)
        if args.dry_run:
            logger.info("[dry-run] 実行せず終了します")
            return 0

        conn.executescript(ddl)
        conn.commit()

        after = existing(conn)
        created = sorted(after - before)
        logger.info("作成しました: %s", created)
        missing_after = [o for o in TARGET_OBJECTS if o not in after]
        if missing_after:
            logger.error("作成されなかったオブジェクトがあります: %s", missing_after)
            return 1
        logger.info("integrity_check: %s", conn.execute("pragma integrity_check").fetchone()[0])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
