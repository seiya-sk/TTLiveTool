import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db


def make_old_schema_conn() -> sqlite3.Connection:
    """A DB in the pre-migration shape: raw_payload column still on
    live_events, old single-column indexes, no live_event_raw_payloads
    table -- what every real DB recorded before this migration looks like."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE streamers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            tiktok_account_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE live_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            streamer_id INTEGER NOT NULL REFERENCES streamers(id),
            title TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'live',
            end_detection_type TEXT
        );
        CREATE TABLE live_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            live_session_id INTEGER NOT NULL REFERENCES live_sessions(id),
            event_type TEXT NOT NULL,
            user_id TEXT,
            user_nickname TEXT,
            payload TEXT,
            raw_payload TEXT,
            occurred_at TEXT NOT NULL
        );
        CREATE INDEX idx_live_events_session ON live_events(live_session_id);
        CREATE INDEX idx_live_events_type ON live_events(event_type);
        """
    )
    conn.commit()
    return conn


def seed_events(conn: sqlite3.Connection) -> tuple[int, int]:
    cursor = conn.execute(
        "INSERT INTO streamers (name, tiktok_account_id, created_at) VALUES (?, ?, ?)",
        ("Streamer", "streamer1", "2026-01-01T00:00:00+00:00"),
    )
    streamer_id = cursor.lastrowid
    cursor = conn.execute(
        "INSERT INTO live_sessions (streamer_id, started_at, status) VALUES (?, ?, 'ended')",
        (streamer_id, "2026-01-01T00:00:00+00:00"),
    )
    session_id = cursor.lastrowid

    gift_id = conn.execute(
        "INSERT INTO live_events (live_session_id, event_type, user_id, user_nickname, payload, raw_payload, occurred_at) "
        "VALUES (?, 'gift', 'u1', 'Gifter', ?, ?, ?)",
        (
            session_id,
            json.dumps({"diamond_count": 5, "repeat_count": 1, "streaking": False}),
            json.dumps({"log_id": "abc123", "user": {"nested": "big-object"}}),
            "2026-01-01T00:00:00+00:00",
        ),
    ).lastrowid
    comment_id = conn.execute(
        "INSERT INTO live_events (live_session_id, event_type, user_id, user_nickname, payload, raw_payload, occurred_at) "
        "VALUES (?, 'comment', 'u2', 'Commenter', ?, ?, ?)",
        (
            session_id,
            json.dumps({"comment": "hi", "gifter_level": None}),
            json.dumps({"some": "raw comment data"}),
            "2026-01-01T00:00:01+00:00",
        ),
    ).lastrowid
    conn.commit()
    return gift_id, comment_id


def test_migration_moves_all_raw_payload_data_without_loss():
    conn = make_old_schema_conn()
    gift_id, comment_id = seed_events(conn)

    db.init_schema(conn)

    assert db.get_raw_payload(conn, gift_id) == {"log_id": "abc123", "user": {"nested": "big-object"}}
    assert db.get_raw_payload(conn, comment_id) == {"some": "raw comment data"}


def test_migration_drops_raw_payload_column_and_old_indexes():
    conn = make_old_schema_conn()
    seed_events(conn)

    db.init_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(live_events)").fetchall()}
    assert "raw_payload" not in columns

    index_names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='live_events'")
    }
    assert "idx_live_events_session" not in index_names
    assert "idx_live_events_type" not in index_names
    assert "idx_live_events_session_type" in index_names


def test_migration_backfills_log_id_into_gift_payload():
    conn = make_old_schema_conn()
    gift_id, _ = seed_events(conn)

    db.init_schema(conn)

    payload = json.loads(conn.execute("SELECT payload FROM live_events WHERE id = ?", (gift_id,)).fetchone()[0])
    assert payload["log_id"] == "abc123"
    assert payload["diamond_count"] == 5  # pre-existing fields untouched


def test_migration_preserves_row_counts():
    conn = make_old_schema_conn()
    seed_events(conn)
    before = conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0]

    db.init_schema(conn)

    after_events = conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0]
    after_raw = conn.execute("SELECT COUNT(*) FROM live_event_raw_payloads").fetchone()[0]
    assert after_events == before
    assert after_raw == before


def test_migration_is_idempotent_when_run_twice():
    conn = make_old_schema_conn()
    gift_id, comment_id = seed_events(conn)

    db.init_schema(conn)
    db.init_schema(conn)  # must not raise (e.g. primary key conflict) or duplicate rows

    count = conn.execute("SELECT COUNT(*) FROM live_event_raw_payloads").fetchone()[0]
    assert count == 2
    assert db.get_raw_payload(conn, gift_id) == {"log_id": "abc123", "user": {"nested": "big-object"}}
    assert db.get_raw_payload(conn, comment_id) == {"some": "raw comment data"}


def test_init_schema_on_already_new_schema_db_is_a_noop():
    # A DB created fresh (never had the old raw_payload column) shouldn't
    # trip the migration path at all.
    conn = db.connect(":memory:")
    db.init_schema(conn)
    db.init_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(live_events)").fetchall()}
    assert "raw_payload" not in columns


def test_migration_adds_streamer_archive_columns():
    # make_old_schema_conn's streamers table predates archived/archived_at
    # (design doc 7-1, added alongside the ライバー管理 screen).
    conn = make_old_schema_conn()
    conn.execute(
        "INSERT INTO streamers (name, tiktok_account_id, created_at) VALUES ('S', 's1', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    db.init_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(streamers)").fetchall()}
    assert "archived" in columns
    assert "archived_at" in columns
    row = conn.execute("SELECT archived, archived_at FROM streamers WHERE tiktok_account_id = 's1'").fetchone()
    assert row[0] == 0  # pre-existing rows backfilled to "not archived", not left NULL
    assert row[1] is None


def test_migration_adds_streamer_archive_columns_idempotently():
    conn = make_old_schema_conn()
    db.init_schema(conn)
    db.init_schema(conn)  # must not raise (e.g. duplicate column error)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(streamers)").fetchall()}
    assert "archived" in columns


def test_migration_adds_streamer_avatar_column():
    # make_old_schema_conn's streamers table predates avatar_path (added
    # alongside fetch_avatars.py).
    conn = make_old_schema_conn()
    conn.execute(
        "INSERT INTO streamers (name, tiktok_account_id, created_at) VALUES ('S', 's1', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    db.init_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(streamers)").fetchall()}
    assert "avatar_path" in columns
    row = conn.execute("SELECT avatar_path FROM streamers WHERE tiktok_account_id = 's1'").fetchone()
    assert row[0] is None


def test_migration_adds_streamer_avatar_column_idempotently():
    conn = make_old_schema_conn()
    db.init_schema(conn)
    db.init_schema(conn)  # must not raise (e.g. duplicate column error)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(streamers)").fetchall()}
    assert "avatar_path" in columns
