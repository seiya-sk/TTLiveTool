import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db


def make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_get_or_create_streamer_is_idempotent():
    conn = make_conn()
    id1 = db.get_or_create_streamer(conn, "example_user")
    id2 = db.get_or_create_streamer(conn, "example_user")
    assert id1 == id2


def test_create_live_session_and_insert_event():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)

    db.insert_event(
        conn,
        session_id,
        "comment",
        user_id="123",
        user_nickname="Alice",
        payload={"comment": "hello"},
        raw_payload={"raw": "data"},
    )

    row = conn.execute(
        "SELECT event_type, user_nickname, payload FROM live_events WHERE live_session_id = ?",
        (session_id,),
    ).fetchone()
    assert row[0] == "comment"
    assert row[1] == "Alice"
    assert "hello" in row[2]


def test_insert_event_stores_raw_payload_in_separate_table():
    # raw_payload lives in live_event_raw_payloads, not live_events -- see
    # db.py's live_events comment for why (read performance: raw_payload is
    # 100-500x bigger than the rest of a row).
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)

    event_id = db.insert_event(
        conn, session_id, "comment",
        user_id="123", user_nickname="Alice",
        payload={"comment": "hello"},
        raw_payload={"raw": "data", "nested": {"a": 1}},
    )

    columns = {row[1] for row in conn.execute("PRAGMA table_info(live_events)").fetchall()}
    assert "raw_payload" not in columns

    assert db.get_raw_payload(conn, event_id) == {"raw": "data", "nested": {"a": 1}}


def test_get_raw_payload_returns_none_for_unknown_event():
    conn = make_conn()
    assert db.get_raw_payload(conn, 999999) is None


# --- get_session_data_volume (Phase 5 cost-model measurement) -------------


def test_get_session_data_volume_counts_events_and_bytes():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)

    db.insert_event(
        conn, session_id, "comment",
        user_id="123", user_nickname="Alice",
        payload={"comment": "hello"},
        raw_payload={"raw": "data"},
    )
    db.insert_event(
        conn, session_id, "gift",
        user_id="456", user_nickname="Bob",
        payload={"gift_id": 1, "diamond_count": 5},
        raw_payload={"raw": "bigger payload here"},
    )

    volume = db.get_session_data_volume(conn, session_id)

    assert volume["event_count"] == 2
    assert volume["payload_bytes"] > 0
    assert volume["raw_payload_bytes"] > 0
    assert volume["total_bytes"] == volume["payload_bytes"] + volume["raw_payload_bytes"]


def test_get_session_data_volume_counts_multibyte_utf8_by_bytes_not_characters():
    # Japanese text is the common case for this project (usernames/comments)
    # -- LENGTH() on a TEXT column in SQLite counts characters, not bytes,
    # so this must CAST to BLOB or a 3-byte-per-char comment would be
    # undercounted 3x.
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)

    japanese_text = "こんにちは"  # 5 chars, 15 bytes in UTF-8
    db.insert_event(
        conn, session_id, "comment",
        user_id="123", user_nickname="Alice",
        payload={"comment": japanese_text},
        raw_payload={},
    )

    volume = db.get_session_data_volume(conn, session_id)

    assert volume["payload_bytes"] >= len(japanese_text.encode("utf-8"))


def test_get_session_data_volume_computes_duration_from_session_timestamps():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)
    db.insert_event(
        conn, session_id, "comment", user_id=None, user_nickname=None, payload={}, raw_payload={}
    )
    db.end_session(conn, session_id, "manual")

    volume = db.get_session_data_volume(conn, session_id)

    assert volume["duration_sec"] is not None
    assert volume["duration_sec"] >= 0


def test_get_session_data_volume_handles_a_session_with_no_events():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)

    volume = db.get_session_data_volume(conn, session_id)

    assert volume["event_count"] == 0
    assert volume["payload_bytes"] == 0
    assert volume["raw_payload_bytes"] == 0
    assert volume["total_bytes"] == 0


def test_get_session_data_volume_handles_an_unknown_session_id():
    conn = make_conn()
    volume = db.get_session_data_volume(conn, 999999)

    assert volume["event_count"] == 0
    assert volume["total_bytes"] == 0
    assert volume["duration_sec"] is None


def test_end_session_sets_status_and_detection_type():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)

    db.end_session(conn, session_id, "auto")

    row = conn.execute(
        "SELECT status, end_detection_type, ended_at FROM live_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    assert row[0] == "ended"
    assert row[1] == "auto"
    assert row[2] is not None


def test_recover_stale_live_sessions_corrects_leftover_live_rows():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    stale_id = db.create_live_session(conn, streamer_id)  # left 'live' by a simulated crash
    ended_id = db.create_live_session(conn, streamer_id)
    db.end_session(conn, ended_id, "manual")  # already cleanly ended, must be untouched

    recovered = db.recover_stale_live_sessions(conn)

    assert recovered == [stale_id]
    stale_row = conn.execute(
        "SELECT status, end_detection_type, ended_at FROM live_sessions WHERE id = ?", (stale_id,)
    ).fetchone()
    # 2026-09-01の設計変更: 'error' ではなく ended/'interrupted' にする。
    #  - 'error' は cleanup_raw_payloads が生ペイロードを永久保護する区分で、
    #    計画的な再起動のたびに増えるとディスクを食い続ける
    #  - status の値集合を増やさないのでダッシュボード側は変更不要
    #  - 'interrupted' は find_resumable_session の再開対象になり、同じ
    #    room_id のライブが続いていれば同じセッションに書き戻せる
    assert stale_row[0] == "ended"
    assert stale_row[1] == "interrupted"
    assert stale_row[2] is not None

    ended_row = conn.execute(
        "SELECT status, end_detection_type FROM live_sessions WHERE id = ?", (ended_id,)
    ).fetchone()
    assert ended_row[0] == "ended"
    assert ended_row[1] == "manual"


def test_recover_stale_live_sessions_returns_empty_when_nothing_stale():
    conn = make_conn()
    assert db.recover_stale_live_sessions(conn) == []


def test_new_streamer_is_not_archived_by_default():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    row = db.list_streamers(conn)[0]
    assert row["id"] == streamer_id
    assert row["archived"] is False
    assert row["archived_at"] is None


def test_archive_streamer_sets_flag_and_timestamp_without_deleting_row():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)  # past data that must survive archiving

    db.archive_streamer(conn, streamer_id)

    row = conn.execute("SELECT archived, archived_at FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
    assert row[0] == 1
    assert row[1] is not None
    # the historical session row is untouched -- logical delete only (design doc 7-1)
    session_row = conn.execute("SELECT id FROM live_sessions WHERE id = ?", (session_id,)).fetchone()
    assert session_row is not None


def test_unarchive_streamer_clears_flag_and_timestamp():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    db.archive_streamer(conn, streamer_id)

    db.unarchive_streamer(conn, streamer_id)

    row = conn.execute("SELECT archived, archived_at FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
    assert row[0] == 0
    assert row[1] is None


def test_set_streamer_avatar_path_updates_row():
    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")

    db.set_streamer_avatar_path(conn, streamer_id, "data/avatars/example_user.webp")

    row = conn.execute("SELECT avatar_path FROM streamers WHERE id = ?", (streamer_id,)).fetchone()
    assert row[0] == "data/avatars/example_user.webp"


def test_list_streamers_include_archived_false_excludes_archived_rows():
    conn = make_conn()
    active_id = db.get_or_create_streamer(conn, "active_user")
    archived_id = db.get_or_create_streamer(conn, "archived_user")
    db.archive_streamer(conn, archived_id)

    visible_ids = {row["id"] for row in db.list_streamers(conn, include_archived=False)}
    all_ids = {row["id"] for row in db.list_streamers(conn, include_archived=True)}

    assert visible_ids == {active_id}
    assert all_ids == {active_id, archived_id}
