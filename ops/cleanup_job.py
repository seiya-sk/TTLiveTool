#!/usr/bin/env python3
"""定期実行用の raw_payload 掃除ジョブ(長期テストの録画プロセスと同居する前提)。

tiktok_monitor.cleanup_raw_payloads をそのまま使いつつ、常時稼働中の
録画プロセスと安全に共存させるための3点を足している:

  1. セッション単位で1件ずつ削除してコミットする。
     モジュール本体の cleanup_raw_payloads() は対象セッションを1本の
     DELETE でまとめて消すため、対象が多いと書き込みロックを長時間
     保持する。録画側の client.py:_record_event -> db.insert_event は
     ロック待ちの再試行を持たず、busy_timeout(既定5秒)を超えると
     例外が上位の再接続ロジックまで伝播して不要な切断を起こす。
     1セッションずつに割ることで、各トランザクションを短く保つ。

  2. このジョブ側の busy_timeout を 30 秒に延ばす。
     競合したときに「録画を待たせる」のではなく「掃除が待つ」向きに
     倒す。掃除は遅れても実害がないが、録画の中断は実害がある。

  3. 実行結果を1行のJSONで出す(削除行数・回収容量・DBサイズ)。
     将来 Chatwork 通知に食わせやすいよう、人間可読ログとは別に
     機械可読な1行を必ず最後に出力する。

注意: SQLite の DELETE ではファイルは縮まない(この DB は
auto_vacuum=NONE)。解放されたページは freelist に入り、以降の
INSERT で再利用される。つまりファイルサイズは「減る」のではなく
「頭打ちになる」。VACUUM は排他ロックで全体を書き直すため、録画中の
DB に対しては実行しない。
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor import db
from tiktok_monitor import cleanup_raw_payloads as cleanup_module

logger = logging.getLogger("cleanup_job")

BUSY_TIMEOUT_MS = 30_000


def _db_stats(conn, db_path: str) -> dict:
    page_size = conn.execute("pragma page_size").fetchone()[0]
    return {
        "db_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
        "wal_bytes": os.path.getsize(db_path + "-wal") if os.path.exists(db_path + "-wal") else 0,
        "page_size": page_size,
        "page_count": conn.execute("pragma page_count").fetchone()[0],
        "freelist_pages": conn.execute("pragma freelist_count").fetchone()[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--retention-days", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    started = time.monotonic()

    conn = db.connect(args.db_path)
    # init_schema() は呼ばない。スキーマは録画プロセスが作成済みで、
    # CREATE TABLE IF NOT EXISTS でも書き込みロックを取るため。
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

    try:
        before = _db_stats(conn, args.db_path)
        eligible = cleanup_module.find_eligible_session_ids(conn, args.retention_days)

        total_rows = 0

        worst_lock_ms = 0.0

        total_batches = 0
        done_sessions = []
        skipped = []
        for sid in eligible:
            # 1セッションずつ。ここでロック保持時間を短く保つ。
            result = cleanup_module.cleanup_raw_payloads_for_session_ids(
                conn, [sid], dry_run=args.dry_run
            )
            total_rows += result["rows"]
            # バッチのロック保持時間(実測)。100ms を超えていたらバッチが
            # 大きすぎるサイン -- 録画側の書き込みを待たせている。
            if result.get("worst_lock_ms"):
                worst_lock_ms = max(worst_lock_ms, result["worst_lock_ms"])
                total_batches += result.get("batches", 0)
            done_sessions.extend(result["session_ids"])
            skipped.extend(result["skipped"])

        after = _db_stats(conn, args.db_path)
    finally:
        conn.close()

    freed_pages = after["freelist_pages"] - before["freelist_pages"]
    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "job": "raw_payload_cleanup",
        "dry_run": args.dry_run,
        "retention_days": args.retention_days,
        "sessions_eligible": len(eligible),
        "sessions_cleaned": len(done_sessions),
        "rows_deleted": total_rows,
        "reclaimed_bytes_est": max(0, freed_pages) * before["page_size"],
        "db_bytes_before": before["db_bytes"],
        "db_bytes_after": after["db_bytes"],
        "wal_bytes_after": after["wal_bytes"],
        "freelist_pages_after": after["freelist_pages"],
        "elapsed_sec": round(time.monotonic() - started, 3),
        # 1バッチあたりの最大ロック保持時間。録画側を待たせた時間の上限で、
        # ここが100msを超えていたらバッチが大きすぎる。
        "worst_lock_ms": round(worst_lock_ms, 1),
        "batches": total_batches,
        "skipped": skipped,
    }

    def mb(b: int) -> str:
        return f"{b / 1048576:.1f}MB"

    logger.info(
        "%sraw_payload cleanup: retention=%dd, %d/%d session(s) cleaned, %d row(s) deleted, "
        "reclaimed~%s, db=%s (wal=%s), %.1fs, ロック最大 %.0fms/%dバッチ",
        "[dry-run] " if args.dry_run else "",
        summary["retention_days"],
        summary["sessions_cleaned"], summary["sessions_eligible"], summary["rows_deleted"],
        mb(summary["reclaimed_bytes_est"]), mb(summary["db_bytes_after"]),
        mb(summary["wal_bytes_after"]), summary["elapsed_sec"],
        summary["worst_lock_ms"], summary["batches"],
    )
    if skipped:
        logger.warning("skipped: %s", skipped)

    # Chatwork通知等に食わせる用の機械可読1行
    print("CLEANUP_SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
