import json
import os
import time
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
-- archived is a logical-delete flag (design doc 7-1: physical delete is
-- never done, so past live_sessions/live_events for an archived streamer
-- stay fully intact and browsable via direct link -- archiving only hides
-- the streamer from the roster/counts, e.g. dashboard queries.ts's
-- getStreamerList/countStreamers).
CREATE TABLE IF NOT EXISTS streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    tiktok_account_id TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    -- 一時的に巡回・録画の対象から外すフラグ。**archived とは別の軸**。
    -- 状態は3つ: 有効(archived=0, enabled=1) / 無効(archived=0, enabled=0)
    --            アーカイブ(archived=1。enabled は意味を持たない)
    --
    -- archived と統合して status 1列にすることも検討したが、既存の
    -- `archived = 0` フィルタが「通常の一覧(有効+無効)」の意味を
    -- そのまま保てるため、こちらを採った。書き換え範囲が小さく、
    -- 稼働中の録画プロセスに触れずに移行できる。
    enabled INTEGER NOT NULL DEFAULT 1,
    -- Path to a locally-cached copy of the streamer's TikTok avatar image
    -- (see fetch_avatars.py) -- NULL until a fetch succeeds. Never a live
    -- CDN URL: TikTok's avatar URLs are signed and, per fetch_avatars.py's
    -- module docstring, not something this app wants to depend on staying
    -- valid indefinitely.
    avatar_path TEXT
);

CREATE TABLE IF NOT EXISTS live_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer_id INTEGER NOT NULL REFERENCES streamers(id),
    title TEXT,
    -- TikTok's own room id for this broadcast. A new broadcast always gets a
    -- new room_id, so it is the only reliable way to tell "the same live we
    -- were already recording" from "a different live by the same streamer".
    -- Used to keep one live in ONE session across an IP handoff and across a
    -- collector restart (see find_resumable_session). NULL for sessions
    -- recorded before this column existed, and NULL is never treated as a
    -- match -- an unknown room_id must not be allowed to glue two unrelated
    -- broadcasts together.
    room_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'live',
    end_detection_type TEXT
);

-- raw_payload deliberately does NOT live on this table -- see
-- live_event_raw_payloads below and _migrate_split_raw_payload's docstring
-- for why (short version: raw_payload averages 20-40KB/row while the rest
-- of a row is ~50-200 bytes, and SQLite reads a row's full on-disk record
-- for ANY column read since storage is row-oriented, not columnar -- so
-- keeping them together made every dashboard/report query pay raw_payload's
-- I/O cost even when it never touched that column. Measured on session 9:
-- ~350x speedup reading viewer_count rows once raw_payload was split out).
CREATE TABLE IF NOT EXISTS live_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    live_session_id INTEGER NOT NULL REFERENCES live_sessions(id),
    event_type TEXT NOT NULL,
    user_id TEXT,
    user_nickname TEXT,
    payload TEXT,
    occurred_at TEXT NOT NULL
);

-- Composite, not two single-column indexes: essentially every query in
-- this codebase (Python report aggregation, dashboard queries.ts) filters
-- on live_session_id AND event_type together, and a composite index lets
-- SQLite seek directly to that (session, type) range instead of scanning
-- one column's index and filtering the other in memory.
CREATE INDEX IF NOT EXISTS idx_live_events_session_type ON live_events(live_session_id, event_type);


-- The full original event, kept for forensics/backfills (e.g.
-- backfill_levels.py) that need fields the curated `payload` doesn't carry.
-- Split into its own table (1:1 with live_events by id) specifically so
-- reading it stays opt-in -- see the live_events comment above. No data is
-- ever dropped by this split; every raw_payload that used to live on
-- live_events is preserved here in full.
CREATE TABLE IF NOT EXISTS live_event_raw_payloads (
    live_event_id INTEGER PRIMARY KEY REFERENCES live_events(id),
    raw_payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    live_session_id INTEGER NOT NULL REFERENCES live_sessions(id),
    captured_at TEXT NOT NULL,
    image_path TEXT NOT NULL,
    ai_analysis TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_screenshots_session ON live_screenshots(live_session_id);

CREATE TABLE IF NOT EXISTS live_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    live_session_id INTEGER NOT NULL REFERENCES live_sessions(id),
    generated_at TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    recommendation_md TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_live_reports_session ON live_reports(live_session_id);

-- Small key/value store for dashboard-editable configuration (USD/JPY rate,
-- Claude token pricing, etc). Shared by the Python side (e.g. fxrate.py)
-- and the Next.js dashboard so both read/write the same source of truth
-- regardless of which one last updated a value.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 進捗通知(系統2)のグループ = 事務所 / 事務所内チーム。ダッシュボードの
-- 通知設定画面から作成・編集する。
--
-- エラー通知(系統1)の宛先はここには入らない。あちらはシステム異常専用で
-- 運用者だけが見るものなので、ルームIDは環境変数
-- CHATWORK_ERROR_ROOM_ID 固定、UIにもDBにも設定を持たせない。
-- 認証情報(CHATWORK_API_TOKEN)も同様に常に環境変数から読む -- DBは検証や
-- バックアップのたびに複製されるので、複製に認証情報を同伴させない。
CREATE TABLE IF NOT EXISTS notification_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL,
    -- Chatwork の [To:...] に展開する account_id のJSON配列。例: '["123","456"]'
    to_account_ids TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    -- その時間帯に配信が1本も無かった場合も送るか。既定は送らない --
    -- 深夜帯に「本日0本」を毎時送っても情報量がないため。
    send_when_empty INTEGER NOT NULL DEFAULT 0,
    -- 通知する時間帯(JST, 24時間表記, start <= hour < end)。事務所ごとに
    -- 稼働時間が違うのでグループ単位。既定の 9-24 は深夜0-6時を落とす。
    notify_start_hour INTEGER NOT NULL DEFAULT 9,
    notify_end_hour INTEGER NOT NULL DEFAULT 24,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- グループ⇔ライバーの多対多。1人のライバーが複数の事務所/チームに属せる。
-- ON DELETE CASCADE が効くのは db.connect() が PRAGMA foreign_keys = ON を
-- 立てているため。streamers 側は archived による論理削除なので、ライバー側
-- からこのCASCADEが誤発火することはない。
CREATE TABLE IF NOT EXISTS notification_group_streamers (
    group_id INTEGER NOT NULL REFERENCES notification_groups(id) ON DELETE CASCADE,
    streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (group_id, streamer_id)
);

CREATE INDEX IF NOT EXISTS idx_notification_group_streamers_streamer
    ON notification_group_streamers(streamer_id);

-- 進捗通知の送信記録。UNIQUE(group_id, window_start) が二重送信防止その
-- ものになっている: 「このグループのこの時間帯」は本質的に一意なので、
-- 汎用のdedupキーを別に持つ必要がない。この制約のおかげで、VPS再起動等で
-- 取りこぼした窓を後から安全に追送できる(送信済みは制約で弾かれる)。
CREATE TABLE IF NOT EXISTS notification_digest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    -- sent / skipped_empty / skipped_quiet_hours / failed
    status TEXT NOT NULL,
    detail TEXT,
    sent_at TEXT NOT NULL,
    UNIQUE (group_id, window_start)
);

CREATE INDEX IF NOT EXISTS idx_notification_digest_log_group
    ON notification_digest_log(group_id, window_start DESC);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 書き込みロックの待ち時間。Python の sqlite3 は既定5秒だが、それでは
# 足りなかった -- 掃除ジョブ(cleanup_raw_payloads)が数万行を1トランザクション
# で消していた間、録画側の INSERT が "database is locked" で失敗し、
# 2026-09-02 の一晩でイベント24件を失った(掃除の所要は最長47.7秒)。
#
# 掃除側はバッチ分割してロック保持を1秒未満に抑えたが、待ち時間そのものも
# 広げておく。待つのは無害(その場で寝るだけ)で、失敗はデータ損失に直結する。
BUSY_TIMEOUT_MS = 30_000

# 書き込みが競合したときの再試行。busy_timeout を使い切ってなお失敗する
# ケース(掃除が長引いた、他プロセスが長いトランザクションを持っている)に
# 備える最後の一段。指数バックオフで待つ。
WRITE_RETRIES = 4
WRITE_RETRY_BASE_SEC = 0.25


class WriteFailed(Exception):
    """再試行しても書き込めなかった。**呼び出し側は必ずデータを退避すること。**

    握りつぶして次のイベントへ進むと、そのイベントは完全に失われる。
    この例外は「捨てた」ではなく「呼び出し側に預けた」ことを意味する。
    """


def connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _is_locked_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def write_with_retry(conn: sqlite3.Connection, operation, *,
                     retries: int = WRITE_RETRIES,
                     base_sec: float = WRITE_RETRY_BASE_SEC):
    """ロック競合で失敗した書き込みを指数バックオフで再試行する。

    ロック以外のエラー(制約違反など)は再試行しても直らないので即座に投げる。
    再試行を使い切ったら WriteFailed を投げる -- 呼び出し側にデータを
    退避させるためで、ここで None を返して黙って進むことはしない。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc):
                raise
            last = exc
            try:
                conn.rollback()      # 途中まで進んだトランザクションを畳む
            except Exception:
                pass
            if attempt < retries:
                time.sleep(base_sec * (2 ** attempt))
    raise WriteFailed(f"{retries + 1}回試しても書き込めませんでした: {last}") from last


def _migrate_split_raw_payload(conn: sqlite3.Connection) -> None:
    """One-time migration for DBs created before raw_payload moved off
    live_events (see the live_events CREATE TABLE comment for why).
    Idempotent: detects the old column via PRAGMA table_info rather than a
    version flag, so it's safe to call unconditionally on every startup --
    a no-op once the column is gone. Also safe if interrupted partway
    through (e.g. killed after the copy but before DROP COLUMN): the copy
    uses INSERT OR IGNORE, so re-running it on the next startup just
    no-ops on rows already copied instead of hitting a primary-key
    conflict.

    Every raw_payload value is copied to live_event_raw_payloads before
    anything is dropped -- no row is ever skipped or lost. Gift rows also
    get their payload backfilled with log_id (extracted from raw_payload
    while it's still on this table) so the gift-dedup queries
    (report/data.py, dashboard queries.ts) can key off payload instead of
    needing to join back to the now-separated raw_payload table for every
    gift row.

    Requires SQLite >= 3.35 for ALTER TABLE ... DROP COLUMN (the Python
    stdlib's bundled sqlite3 has been 3.42+ for a while)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(live_events)").fetchall()}
    if "raw_payload" not in columns:
        return

    conn.execute(
        "INSERT OR IGNORE INTO live_event_raw_payloads (live_event_id, raw_payload) "
        "SELECT id, raw_payload FROM live_events WHERE raw_payload IS NOT NULL"
    )
    conn.execute(
        """
        UPDATE live_events
        SET payload = json_set(COALESCE(payload, '{}'), '$.log_id', json_extract(raw_payload, '$.log_id'))
        WHERE event_type = 'gift'
        """
    )
    conn.execute("DROP INDEX IF EXISTS idx_live_events_session")
    conn.execute("DROP INDEX IF EXISTS idx_live_events_type")
    conn.execute("ALTER TABLE live_events DROP COLUMN raw_payload")
    conn.commit()


def _migrate_add_streamer_archive_columns(conn: sqlite3.Connection) -> None:
    """One-time migration for DBs created before streamers.archived existed.
    Idempotent via PRAGMA table_info, same pattern as
    _migrate_split_raw_payload. New rows already get archived=0 from the
    schema's DEFAULT; this only needs to run for DBs whose streamers table
    predates the column."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(streamers)").fetchall()}
    if "archived" not in columns:
        conn.execute("ALTER TABLE streamers ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "archived_at" not in columns:
        conn.execute("ALTER TABLE streamers ADD COLUMN archived_at TEXT")
    if "enabled" not in columns:
        # 既存行はすべて有効として扱う。無効という状態はこの列を足すまで
        # 存在しなかったので、これ以外の解釈はない。
        conn.execute("ALTER TABLE streamers ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    conn.commit()


def _migrate_add_streamer_avatar_column(conn: sqlite3.Connection) -> None:
    """One-time migration for DBs created before streamers.avatar_path
    existed. Idempotent via PRAGMA table_info, same pattern as
    _migrate_add_streamer_archive_columns."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(streamers)").fetchall()}
    if "avatar_path" not in columns:
        conn.execute("ALTER TABLE streamers ADD COLUMN avatar_path TEXT")
    conn.commit()


def _migrate_add_live_session_room_id(conn: sqlite3.Connection) -> None:
    """One-time migration for DBs created before live_sessions.room_id
    existed. Idempotent via PRAGMA table_info, same pattern as
    _migrate_add_streamer_avatar_column."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(live_sessions)").fetchall()}
    if "room_id" not in columns:
        conn.execute("ALTER TABLE live_sessions ADD COLUMN room_id TEXT")
    # インデックスは ALTER の後、かつ if の外で張る。SCHEMA 側に置くと、
    # 既存DBでは CREATE TABLE IF NOT EXISTS が何もしないまま CREATE INDEX が
    # 走り "no such column: room_id" で init_schema 全体が落ちる
    # (テストで検出。本番DBの移行がそのまま失敗するところだった)。
    # if の外なのは、新規DBでは列が最初からあって ALTER 側を通らないため。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_sessions_room "
        "ON live_sessions(streamer_id, room_id, ended_at)"
    )
    conn.commit()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_split_raw_payload(conn)
    _migrate_add_streamer_archive_columns(conn)
    _migrate_add_streamer_avatar_column(conn)
    _migrate_add_live_session_room_id(conn)


def get_or_create_streamer(conn: sqlite3.Connection, tiktok_account_id: str, name: str | None = None) -> int:
    row = conn.execute(
        "SELECT id FROM streamers WHERE tiktok_account_id = ?", (tiktok_account_id,)
    ).fetchone()
    if row:
        return row[0]
    cursor = conn.execute(
        "INSERT INTO streamers (name, tiktok_account_id, created_at) VALUES (?, ?, ?)",
        (name or tiktok_account_id, tiktok_account_id, utc_now_iso()),
    )
    conn.commit()
    return cursor.lastrowid


def get_streamer_avatar_path(conn: sqlite3.Connection, streamer_id: int) -> str | None:
    row = conn.execute("SELECT avatar_path FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
    return row[0] if row else None


def set_streamer_avatar_path(conn: sqlite3.Connection, streamer_id: int, avatar_path: str) -> None:
    conn.execute("UPDATE streamers SET avatar_path = ? WHERE id = ?", (avatar_path, streamer_id))
    conn.commit()


def archive_streamer(conn: sqlite3.Connection, streamer_id: int) -> None:
    """Logical delete only (design doc 7-1) -- never physically removes the
    row, so every live_sessions/live_events row referencing it stays intact.
    Recording a new session for an archived streamer would still work (no
    FK/check constraint blocks it), but watch.py doesn't currently consult
    this flag at all -- see module docstring in the navigation design doc's
    5-3 for that future wiring."""
    conn.execute(
        "UPDATE streamers SET archived = 1, archived_at = ? WHERE id = ?", (utc_now_iso(), streamer_id)
    )
    conn.commit()


def unarchive_streamer(conn: sqlite3.Connection, streamer_id: int) -> None:
    conn.execute("UPDATE streamers SET archived = 0, archived_at = NULL WHERE id = ?", (streamer_id,))
    conn.commit()


def set_streamer_enabled(conn: sqlite3.Connection, streamer_id: int, enabled: bool) -> None:
    """一時的に巡回・録画の対象から外す/戻す。**アーカイブとは別の軸**で、
    通常の一覧には表示され続ける。過去データには一切触れない。"""
    conn.execute("UPDATE streamers SET enabled = ? WHERE id = ?", (1 if enabled else 0, streamer_id))
    conn.commit()


def list_streamers(conn: sqlite3.Connection, include_archived: bool = True) -> list[dict]:
    query = ("SELECT id, name, tiktok_account_id, archived, archived_at, created_at, enabled "
             "FROM streamers")
    if not include_archived:
        query += " WHERE archived = 0"
    query += " ORDER BY archived ASC, name ASC"
    rows = conn.execute(query).fetchall()
    return [
        {
            "id": r[0],
            "name": r[1],
            "tiktok_account_id": r[2],
            "archived": bool(r[3]),
            "archived_at": r[4],
            "created_at": r[5],
            "enabled": bool(r[6]),
        }
        for r in rows
    ]


def create_live_session(
    conn: sqlite3.Connection, streamer_id: int, title: str | None = None, room_id: str | None = None
) -> int:
    cursor = conn.execute(
        "INSERT INTO live_sessions (streamer_id, title, room_id, started_at, status) VALUES (?, ?, ?, ?, 'live')",
        (streamer_id, title, room_id, utc_now_iso()),
    )
    conn.commit()
    return cursor.lastrowid


# 同じライブとみなして再開してよい終了理由。
#   auto        -- 無イベントでこちらが打ち切ったもの(= 誤検知の可能性がある)
#   interrupted -- プロセスが落ちた/再起動した(= ライブの終了とは無関係)
# 逆に live_end / normal_closure / manual は「終わったと分かっている」ので
# 決して再開しない。ここを緩めると、終わったライブに次のライブのイベントを
# 書き込む取り返しのつかない汚染になる。
RESUMABLE_END_TYPES = ("auto", "interrupted")


def find_resumable_session(
    conn: sqlite3.Connection, streamer_id: int, room_id: str | None, within_sec: float
) -> int | None:
    """同じライブの続きとして書き込んでよいセッションIDを返す。無ければ None。

    room_id が None のときは常に None を返す -- 判定できないときは繋がない
    側に倒す。分割は後から繋ぎ直せるが、別ライブの誤結合は復元できない。

    条件を4つ重ねている:
      1. 同じ streamer   (別人を繋がない)
      2. 同じ room_id    (別ライブを繋がない -- 主条件)
      3. ended_at が within_sec 以内 (昨日の同名ライブを繋がない)
      4. end_detection_type が RESUMABLE_END_TYPES (明示終了は繋がない)
    """
    if not room_id:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_sec)).isoformat()
    placeholders = ",".join("?" for _ in RESUMABLE_END_TYPES)
    row = conn.execute(
        f"""
        SELECT id FROM live_sessions
        WHERE streamer_id = ?
          AND room_id = ?
          AND ended_at IS NOT NULL
          AND ended_at >= ?
          AND end_detection_type IN ({placeholders})
        ORDER BY ended_at DESC
        LIMIT 1
        """,
        (streamer_id, room_id, cutoff, *RESUMABLE_END_TYPES),
    ).fetchone()
    return row[0] if row else None


def resume_session(conn: sqlite3.Connection, live_session_id: int) -> None:
    """終了扱いになっていたセッションを再び 'live' に戻す。started_at は
    元のまま残す -- 配信の実際の開始時刻であって、再開した時刻ではない。"""
    conn.execute(
        """
        UPDATE live_sessions
        SET status = 'live', ended_at = NULL, end_detection_type = NULL
        WHERE id = ?
        """,
        (live_session_id,),
    )
    conn.commit()


def set_session_room_id(conn: sqlite3.Connection, live_session_id: int, room_id: str | None) -> None:
    """接続後に判明した room_id を後から埋める(既に入っていれば何もしない)。"""
    if not room_id:
        return
    conn.execute(
        "UPDATE live_sessions SET room_id = ? WHERE id = ? AND (room_id IS NULL OR room_id = '')",
        (room_id, live_session_id),
    )
    conn.commit()


def insert_event(
    conn: sqlite3.Connection,
    live_session_id: int,
    event_type: str,
    user_id: str | None,
    user_nickname: str | None,
    payload: dict,
    raw_payload: dict,
    occurred_at: str | None = None,
) -> int:
    # イベント本体と生ペイロードは1つの書き込み単位として再試行する。
    # 片方だけ入って片方が落ちると、生ペイロードの無いイベント行が残る。
    def _insert():
        return _insert_event_once(
            conn, live_session_id, event_type, user_id, user_nickname,
            payload, raw_payload, occurred_at,
        )

    return write_with_retry(conn, _insert)


def _insert_event_once(
    conn: sqlite3.Connection,
    live_session_id: int,
    event_type: str,
    user_id: str | None,
    user_nickname: str | None,
    payload: dict,
    raw_payload: dict,
    occurred_at: str | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO live_events
            (live_session_id, event_type, user_id, user_nickname, payload, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            live_session_id,
            event_type,
            user_id,
            user_nickname,
            json.dumps(payload, ensure_ascii=False, default=str),
            occurred_at or utc_now_iso(),
        ),
    )
    event_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO live_event_raw_payloads (live_event_id, raw_payload) VALUES (?, ?)",
        (event_id, json.dumps(raw_payload, ensure_ascii=False, default=str)),
    )
    conn.commit()
    return event_id


def get_raw_payload(conn: sqlite3.Connection, live_event_id: int) -> dict | None:
    row = conn.execute(
        "SELECT raw_payload FROM live_event_raw_payloads WHERE live_event_id = ?", (live_event_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def get_session_data_volume(conn: sqlite3.Connection, live_session_id: int) -> dict:
    """Estimated recorded data volume for one live session -- Phase 5's
    "how much does one stream actually cost to record" question
    (docs/phase5-1ip-measurement-spec.md's cost model used a placeholder
    5MB/stream; this replaces the guess with a real measurement).

    CAST(... AS BLOB) before LENGTH() so multi-byte UTF-8 (Japanese
    usernames/comments are the common case here) counts actual bytes, not
    SQLite's default character count for TEXT columns."""
    row = conn.execute(
        """
        SELECT
            ls.started_at,
            ls.ended_at,
            COUNT(le.id) AS event_count,
            COALESCE(SUM(LENGTH(CAST(le.payload AS BLOB))), 0) AS payload_bytes,
            COALESCE(SUM(LENGTH(CAST(rp.raw_payload AS BLOB))), 0) AS raw_payload_bytes
        FROM live_sessions ls
        LEFT JOIN live_events le ON le.live_session_id = ls.id
        LEFT JOIN live_event_raw_payloads rp ON rp.live_event_id = le.id
        WHERE ls.id = ?
        GROUP BY ls.id
        """,
        (live_session_id,),
    ).fetchone()
    if row is None:
        return {
            "event_count": 0,
            "payload_bytes": 0,
            "raw_payload_bytes": 0,
            "total_bytes": 0,
            "duration_sec": None,
        }
    started_at, ended_at, event_count, payload_bytes, raw_payload_bytes = row
    duration_sec = None
    if started_at and ended_at:
        duration_sec = (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds()
    return {
        "event_count": event_count,
        "payload_bytes": payload_bytes,
        "raw_payload_bytes": raw_payload_bytes,
        "total_bytes": payload_bytes + raw_payload_bytes,
        "duration_sec": duration_sec,
    }


def end_session(conn: sqlite3.Connection, live_session_id: int, end_detection_type: str) -> None:
    conn.execute(
        """
        UPDATE live_sessions
        SET status = 'ended', ended_at = ?, end_detection_type = ?
        WHERE id = ?
        """,
        (utc_now_iso(), end_detection_type, live_session_id),
    )
    conn.commit()


def get_session_started_at(conn: sqlite3.Connection, live_session_id: int) -> str | None:
    row = conn.execute(
        "SELECT started_at FROM live_sessions WHERE id = ?", (live_session_id,)
    ).fetchone()
    return row[0] if row else None


def session_has_screenshot(conn: sqlite3.Connection, live_session_id: int) -> bool:
    """このセッションのスクリーンショットが既に1枚でもあるか。

    live_screenshots に一意制約が無いので、撮る側がここを見ないと同じ
    セッションに複数枚入る。プロセスをまたいだ多重起動(再起動で再開した
    ランナーが、前のプロセスが撮った1枚を知らずにもう1枚撮る)を防げるのは
    この判定だけ -- メモリ上のフラグはプロセス境界を越えられない。
    """
    return conn.execute(
        "SELECT 1 FROM live_screenshots WHERE live_session_id = ? LIMIT 1", (live_session_id,)
    ).fetchone() is not None


def insert_screenshot(
    conn: sqlite3.Connection,
    live_session_id: int,
    image_path: str,
    captured_at: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO live_screenshots (live_session_id, captured_at, image_path) VALUES (?, ?, ?)",
        (live_session_id, captured_at or utc_now_iso(), image_path),
    )
    conn.commit()
    return cursor.lastrowid


def insert_report(
    conn: sqlite3.Connection,
    live_session_id: int,
    summary_json: dict,
    recommendation_md: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO live_reports (live_session_id, generated_at, summary_json, recommendation_md) VALUES (?, ?, ?, ?)",
        (
            live_session_id,
            utc_now_iso(),
            json.dumps(summary_json, ensure_ascii=False, default=str),
            recommendation_md,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def mark_session_error(conn: sqlite3.Connection, live_session_id: int) -> None:
    conn.execute(
        "UPDATE live_sessions SET status = 'error', ended_at = ? WHERE id = ?",
        (utc_now_iso(), live_session_id),
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT value, updated_at FROM app_settings WHERE key = ?", (key,)).fetchone()
    return {"value": row[0], "updated_at": row[1]} if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now_iso()),
    )
    conn.commit()


def recover_stale_live_sessions(conn: sqlite3.Connection) -> list[int]:
    """A live_sessions row still 'live' when a collector process (main.py /
    watch.py) starts up means the PREVIOUS process didn't shut down cleanly
    (crash, kill -9, power loss) -- manual_end()/idle-timeout never ran, so
    ended_at/end_detection_type were never set. Call this once at startup,
    right after init_schema, to correct any such rows to status='error'
    before anything else touches the DB.

    Assumes only one collector process runs against this DB at a time
    (matches the current single-PC/single-concurrent-session constraint --
    see design doc 5.4 and watch.py's single-IP note). If that assumption
    ever changes, this needs to become scoped to specific streamers rather
    than blanket-correcting every 'live' row, or it could clobber a session
    a different, still-running process is legitimately recording."""
    stale_ids = [row[0] for row in conn.execute("SELECT id FROM live_sessions WHERE status = 'live'").fetchall()]
    for session_id in stale_ids:
        # status='error' ではなく ended/'interrupted' にする理由:
        #   - 'error' は cleanup_raw_payloads が生ペイロードを永久保護する
        #     区分。計画的な再起動のたびに error が増えると、その分の生
        #     ペイロード(20-40KB/行)が消えずにディスクを食い続ける。
        #   - status の値集合を増やさないので、ダッシュボード側の表示
        #     ロジック(queries.ts / StatusBadge)は変更不要。ニュアンスは
        #     end_detection_type が持つ。
        #   - 'interrupted' は find_resumable_session の再開対象。同じ
        #     room_id のライブがまだ続いていれば、同じセッションに書き戻す。
        # ended_at は「補正を実行した時刻」ではなく「そのセッションの最後の
        # イベント時刻」にする。数時間止まっていた場合に、止まっていた間も
        # 配信していたことになってしまうのを防ぐ(進捗通知の延べ配信時間が
        # 実態とずれる)。
        last_event = conn.execute(
            "SELECT MAX(occurred_at) FROM live_events WHERE live_session_id = ?", (session_id,)
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE live_sessions
            SET status = 'ended', ended_at = ?, end_detection_type = 'interrupted'
            WHERE id = ?
            """,
            (last_event or utc_now_iso(), session_id),
        )
    conn.commit()
    return stale_ids
