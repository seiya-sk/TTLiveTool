import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.cleanup_raw_payloads import (
    cleanup_raw_payloads,
    cleanup_raw_payloads_for_session_ids,
    get_retention_days,
)


def make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def make_session(conn, status: str, ended_days_ago: float | None):
    """ended_days_ago=None -> still live (ended_at NULL, matching a real
    'live' session). A real event with raw_payload is attached so tests can
    check whether that row survives cleanup."""
    streamer_id = db.get_or_create_streamer(conn, f"user_{status}_{ended_days_ago}")
    session_id = db.create_live_session(conn, streamer_id)
    ended_at = None
    if ended_days_ago is not None:
        ended_at = (datetime.now(timezone.utc) - timedelta(days=ended_days_ago)).isoformat()
    conn.execute(
        "UPDATE live_sessions SET status = ?, ended_at = ? WHERE id = ?", (status, ended_at, session_id)
    )
    conn.commit()
    event_id = db.insert_event(
        conn, session_id, "comment", "u1", "Nick",
        payload={"comment": "hello"}, raw_payload={"raw": "big log data"},
    )
    return session_id, event_id


def test_get_retention_days_defaults_to_three_when_unset():
    conn = make_conn()
    assert get_retention_days(conn) == 3


def test_get_retention_days_reads_app_setting():
    conn = make_conn()
    db.set_setting(conn, "raw_payload_retention_days", "7")
    assert get_retention_days(conn) == 7


def test_get_retention_days_falls_back_to_default_on_malformed_value():
    conn = make_conn()
    db.set_setting(conn, "raw_payload_retention_days", "not-a-number")
    assert get_retention_days(conn) == 3


def test_error_session_is_excluded_even_when_old():
    # The core safety requirement: an errored recording's raw log must
    # never be purged, no matter how old, so it stays available for debugging.
    conn = make_conn()
    session_id, event_id = make_session(conn, status="error", ended_days_ago=365)

    result = cleanup_raw_payloads(conn, retention_days=3, dry_run=False)

    assert session_id not in result["session_ids"]
    assert result["rows"] == 0
    assert db.get_raw_payload(conn, event_id) is not None  # untouched


def test_live_session_with_null_ended_at_is_excluded():
    conn = make_conn()
    session_id, event_id = make_session(conn, status="live", ended_days_ago=None)

    result = cleanup_raw_payloads(conn, retention_days=0, dry_run=False)

    assert session_id not in result["session_ids"]
    assert db.get_raw_payload(conn, event_id) is not None


def test_recently_ended_session_within_retention_window_is_excluded():
    conn = make_conn()
    session_id, event_id = make_session(conn, status="ended", ended_days_ago=1)

    result = cleanup_raw_payloads(conn, retention_days=3, dry_run=False)

    assert session_id not in result["session_ids"]
    assert db.get_raw_payload(conn, event_id) is not None


def test_session_ended_past_retention_window_is_eligible_and_purged():
    conn = make_conn()
    session_id, event_id = make_session(conn, status="ended", ended_days_ago=10)

    result = cleanup_raw_payloads(conn, retention_days=3, dry_run=False)

    assert session_id in result["session_ids"]
    assert result["rows"] == 1
    assert db.get_raw_payload(conn, event_id) is None  # raw_payload gone


def test_payload_and_live_events_row_survive_cleanup():
    # The whole point: payload (and the rest of live_events) is permanent,
    # only live_event_raw_payloads is ever touched.
    conn = make_conn()
    session_id, event_id = make_session(conn, status="ended", ended_days_ago=10)

    cleanup_raw_payloads(conn, retention_days=3, dry_run=False)

    row = conn.execute(
        "SELECT event_type, user_nickname, payload FROM live_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "comment"
    assert row[1] == "Nick"
    assert "hello" in row[2]


def test_dry_run_reports_counts_without_deleting_anything():
    conn = make_conn()
    session_id, event_id = make_session(conn, status="ended", ended_days_ago=10)

    result = cleanup_raw_payloads(conn, retention_days=3, dry_run=True)

    assert session_id in result["session_ids"]
    assert result["rows"] == 1
    # Nothing was actually deleted.
    assert db.get_raw_payload(conn, event_id) is not None


def test_cleanup_only_purges_eligible_sessions_leaving_others_intact():
    conn = make_conn()
    eligible_id, eligible_event = make_session(conn, status="ended", ended_days_ago=10)
    error_id, error_event = make_session(conn, status="error", ended_days_ago=10)
    live_id, live_event = make_session(conn, status="live", ended_days_ago=None)
    recent_id, recent_event = make_session(conn, status="ended", ended_days_ago=1)

    result = cleanup_raw_payloads(conn, retention_days=3, dry_run=False)

    assert result["session_ids"] == [eligible_id]
    assert db.get_raw_payload(conn, eligible_event) is None
    assert db.get_raw_payload(conn, error_event) is not None
    assert db.get_raw_payload(conn, live_event) is not None
    assert db.get_raw_payload(conn, recent_event) is not None


def test_uses_configured_retention_days_when_not_overridden():
    conn = make_conn()
    db.set_setting(conn, "raw_payload_retention_days", "30")
    session_id, event_id = make_session(conn, status="ended", ended_days_ago=10)  # inside 30-day window

    result = cleanup_raw_payloads(conn, dry_run=True)  # no explicit retention_days -> reads app_settings

    assert result["retention_days"] == 30
    assert session_id not in result["session_ids"]  # 10 days old, not yet past 30-day retention


# --- cleanup_raw_payloads_for_session_ids (targeted, id-based) -----------


def test_targeted_cleanup_purges_only_the_requested_session():
    # The scenario this exists for: purge exactly one session's raw log
    # (e.g. a specific old test session) without a retention-day threshold
    # sweeping in every other session that happens to be at least as old.
    conn = make_conn()
    target_id, target_event = make_session(conn, status="ended", ended_days_ago=1)
    other_id, other_event = make_session(conn, status="ended", ended_days_ago=1)

    result = cleanup_raw_payloads_for_session_ids(conn, [target_id], dry_run=False)

    assert result["session_ids"] == [target_id]
    assert result["rows"] == 1
    assert db.get_raw_payload(conn, target_event) is None
    assert db.get_raw_payload(conn, other_event) is not None  # untouched


def test_targeted_cleanup_still_refuses_error_sessions():
    conn = make_conn()
    error_id, error_event = make_session(conn, status="error", ended_days_ago=100)

    result = cleanup_raw_payloads_for_session_ids(conn, [error_id], dry_run=False)

    assert result["session_ids"] == []
    assert result["skipped"] == [{"id": error_id, "reason": "status=error"}]
    assert db.get_raw_payload(conn, error_event) is not None


def test_targeted_cleanup_still_refuses_live_sessions():
    conn = make_conn()
    live_id, live_event = make_session(conn, status="live", ended_days_ago=None)

    result = cleanup_raw_payloads_for_session_ids(conn, [live_id], dry_run=False)

    assert result["session_ids"] == []
    assert result["skipped"] == [{"id": live_id, "reason": "still live (ended_at is null)"}]
    assert db.get_raw_payload(conn, live_event) is not None


def test_targeted_cleanup_reports_unknown_id_as_skipped():
    conn = make_conn()
    result = cleanup_raw_payloads_for_session_ids(conn, [999999], dry_run=False)

    assert result["session_ids"] == []
    assert result["skipped"] == [{"id": 999999, "reason": "not found"}]


def test_targeted_cleanup_dry_run_does_not_delete():
    conn = make_conn()
    target_id, target_event = make_session(conn, status="ended", ended_days_ago=1)

    result = cleanup_raw_payloads_for_session_ids(conn, [target_id], dry_run=True)

    assert result["session_ids"] == [target_id]
    assert result["rows"] == 1
    assert db.get_raw_payload(conn, target_event) is not None  # nothing actually deleted


def test_targeted_cleanup_payload_and_live_events_survive():
    conn = make_conn()
    target_id, target_event = make_session(conn, status="ended", ended_days_ago=1)

    cleanup_raw_payloads_for_session_ids(conn, [target_id], dry_run=False)

    row = conn.execute("SELECT payload FROM live_events WHERE id = ?", (target_event,)).fetchone()
    assert row is not None
    assert "hello" in row[0]
