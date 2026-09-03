"""Deletes old raw_payload rows to bound storage growth, while keeping the
curated `payload` (and everything else in live_events) forever -- payload is
what the dashboard/reports actually read; raw_payload is the much bigger
(20-40KB/row) forensic copy kept only for occasional debugging/backfills
(see db.py's live_events comment). Only live_event_raw_payloads rows are
ever deleted here; live_events itself is never touched.

Retention is configurable via app_settings (dashboard-editable, same
mechanism as the USD/JPY rate / token pricing -- see fxrate.py), keyed
"raw_payload_retention_days", defaulting to 3 days if unset.

A session is eligible once it's cleanly ended (status='ended', not 'live'
or 'error') and its ended_at is older than the retention window.
status='error' sessions are deliberately excluded forever (until manually
reclassified) so a crashed/errored recording's raw log stays available for
debugging -- this is a permanent safety carve-out, not just "not yet
eligible".

Usage: python -m tiktok_monitor.cleanup_raw_payloads [--db-path PATH]
       [--retention-days N] [--dry-run]
"""
import argparse
import logging
import time
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db

logger = logging.getLogger(__name__)

SETTING_KEY = "raw_payload_retention_days"
DEFAULT_RETENTION_DAYS = 3


def get_retention_days(conn: sqlite3.Connection) -> int:
    row = db.get_setting(conn, SETTING_KEY)
    if row is None:
        return DEFAULT_RETENTION_DAYS
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


# 1トランザクションで消す行数。**ロックの保持時間を短く保つのが目的。**
# 以前は対象セッション全部を1文で消しており、最大19,070行(約430MB)を
# 1トランザクションで処理して書き込みロックを最長47.7秒握っていた。
# その間、録画側の INSERT が busy_timeout(5秒)を超えて失敗し、
# 2026-09-02 の一晩でイベント24件が失われた。
#
# 1行あたり約23KBなので、500行で約11MB。実測でコミットまで1秒未満に収まる。
DELETE_BATCH_ROWS = 500
# バッチ間に挟む休止。SQLite のロック取得は先着順を保証しないので、
# 間を空けずに次のバッチへ行くと掃除側が連続で取り続けうる。
# 録画側に確実に順番が回るようにする。
DELETE_BATCH_PAUSE_SEC = 0.05


def delete_raw_payloads_batched(
    conn: sqlite3.Connection,
    session_ids: list[int],
    batch_rows: int = DELETE_BATCH_ROWS,
    pause_sec: float = DELETE_BATCH_PAUSE_SEC,
) -> int:
    """対象セッションの生ペイロードを小分けにして削除し、削除件数を返す。

    毎バッチ commit する。ロックを短時間しか持たないので、録画側の書き込みが
    待たされても busy_timeout の内側で収まる。

    rowid で消すのが要点。「live_session_id が対象のもの」という条件で
    LIMIT 付き DELETE を繰り返すと、live_events 側は消えないので同じ行が
    何度も選ばれ、進まなくなる。消す対象そのものの rowid を取ってから
    消せば、消えた分だけ確実に前に進む。
    """
    if not session_ids:
        return 0
    placeholders = ",".join("?" for _ in session_ids)
    select_sql = f"""
        SELECT p.rowid
        FROM live_event_raw_payloads p
        JOIN live_events e ON e.id = p.live_event_id
        WHERE e.live_session_id IN ({placeholders})
        LIMIT ?
    """
    deleted = 0
    while True:
        rowids = [r[0] for r in conn.execute(select_sql, (*session_ids, batch_rows))]
        if not rowids:
            break
        marks = ",".join("?" for _ in rowids)
        conn.execute(f"DELETE FROM live_event_raw_payloads WHERE rowid IN ({marks})", rowids)
        conn.commit()
        deleted += len(rowids)
        if pause_sec:
            time.sleep(pause_sec)
    return deleted


def find_eligible_session_ids(conn: sqlite3.Connection, retention_days: int) -> list[int]:
    """Cleanly-ended sessions (status='ended') whose ended_at is older than
    retention_days. status='error' is excluded (protected for debugging,
    not just "too recent"); status='live' is excluded automatically since
    those rows have ended_at IS NULL."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    rows = conn.execute(
        "SELECT id FROM live_sessions WHERE status = 'ended' AND ended_at IS NOT NULL AND ended_at < ?",
        (cutoff,),
    ).fetchall()
    return [r[0] for r in rows]


def _raw_payload_row_count(conn: sqlite3.Connection, session_ids: list[int]) -> int:
    if not session_ids:
        return 0
    placeholders = ",".join("?" for _ in session_ids)
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM live_event_raw_payloads
        WHERE live_event_id IN (SELECT id FROM live_events WHERE live_session_id IN ({placeholders}))
        """,
        session_ids,
    ).fetchone()
    return row[0]


def cleanup_raw_payloads(
    conn: sqlite3.Connection, retention_days: int | None = None, dry_run: bool = False
) -> dict:
    """Returns {"retention_days", "session_ids", "sessions", "rows"}.
    `rows` is always the count that were (or, in dry-run, would be)
    deleted from live_event_raw_payloads -- payload/live_events is never
    touched by this function regardless of dry_run."""
    if retention_days is None:
        retention_days = get_retention_days(conn)

    session_ids = find_eligible_session_ids(conn, retention_days)
    row_count = _raw_payload_row_count(conn, session_ids)

    if not dry_run and session_ids:
        delete_raw_payloads_batched(conn, session_ids)

    return {
        "retention_days": retention_days,
        "session_ids": session_ids,
        "sessions": len(session_ids),
        "rows": row_count,
    }


def cleanup_raw_payloads_for_session_ids(
    conn: sqlite3.Connection, session_ids: list[int], dry_run: bool = False
) -> dict:
    """Directly targets specific sessions by id instead of an age cutoff --
    for a one-off "purge exactly this session, nothing else" operation
    where a retention-day threshold can't express "only this one" (any
    threshold wide enough to include one session also includes every older
    one). Still refuses status='error' and still-live (ended_at IS NULL)
    sessions, and silently skips ids that don't exist -- the "only ever
    touch live_event_raw_payloads for genuinely eligible sessions" rule
    from cleanup_raw_payloads applies here too, not just for the ids the
    caller intended.

    Returns {"session_ids" (the ones actually targeted), "sessions",
    "rows", "skipped" (id/reason pairs for anything requested but not
    touched)}."""
    if not session_ids:
        return {"session_ids": [], "sessions": 0, "rows": 0, "skipped": []}

    placeholders = ",".join("?" for _ in session_ids)
    found = conn.execute(
        f"SELECT id, status, ended_at FROM live_sessions WHERE id IN ({placeholders})", session_ids
    ).fetchall()
    found_ids = {row[0] for row in found}

    eligible_ids = [row[0] for row in found if row[1] != "error" and row[2] is not None]

    skipped = []
    for row in found:
        if row[0] not in eligible_ids:
            reason = "status=error" if row[1] == "error" else "still live (ended_at is null)"
            skipped.append({"id": row[0], "reason": reason})
    for sid in session_ids:
        if sid not in found_ids:
            skipped.append({"id": sid, "reason": "not found"})

    row_count = _raw_payload_row_count(conn, eligible_ids)

    if not dry_run and eligible_ids:
        delete_raw_payloads_batched(conn, eligible_ids)

    return {"session_ids": eligible_ids, "sessions": len(eligible_ids), "rows": row_count, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="data/tts_live_tool.db")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Override app_settings' raw_payload_retention_days for this run only",
    )
    target_group.add_argument(
        "--session-id",
        type=int,
        nargs="+",
        default=None,
        help="Target only these specific session id(s), ignoring the age-based retention rule",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted without deleting")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    conn = db.connect(args.db_path)
    db.init_schema(conn)
    try:
        if args.session_id:
            result = cleanup_raw_payloads_for_session_ids(conn, args.session_id, dry_run=args.dry_run)
            logger.info(
                "%sraw_payload cleanup (targeted): %d session(s), %d row(s) %s",
                "[dry-run] " if args.dry_run else "",
                result["sessions"],
                result["rows"],
                "would be deleted" if args.dry_run else "deleted",
            )
            if result["session_ids"]:
                logger.info("targeted session ids: %s", result["session_ids"])
            if result["skipped"]:
                logger.warning("skipped (not eligible): %s", result["skipped"])
        else:
            result = cleanup_raw_payloads(conn, retention_days=args.retention_days, dry_run=args.dry_run)
            logger.info(
                "%sraw_payload cleanup: retention=%d day(s), %d session(s) eligible, %d row(s) %s",
                "[dry-run] " if args.dry_run else "",
                result["retention_days"],
                result["sessions"],
                result["rows"],
                "would be deleted" if args.dry_run else "deleted",
            )
            if result["session_ids"]:
                logger.info("eligible session ids: %s", result["session_ids"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
