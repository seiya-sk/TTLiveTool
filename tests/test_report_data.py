import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.report.data import aggregate_session_data


def make_session(ended=True):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    streamer_id = db.get_or_create_streamer(conn, "streamer1", name="Streamer One")
    session_id = db.create_live_session(conn, streamer_id, title="test session")
    return conn, session_id


def test_basic_stats_computed_correctly():
    conn, session_id = make_session()

    db.insert_event(conn, session_id, "viewer_count", None, None, {"viewer_count": 100}, {}, occurred_at="2026-01-01T00:00:00+00:00")
    db.insert_event(conn, session_id, "viewer_count", None, None, {"viewer_count": 200}, {}, occurred_at="2026-01-01T00:01:00+00:00")
    db.insert_event(conn, session_id, "like", None, None, {"total_likes": 999}, {}, occurred_at="2026-01-01T00:00:30+00:00")
    db.insert_event(conn, session_id, "like", None, None, {"total_likes": 1500}, {}, occurred_at="2026-01-01T00:02:00+00:00")
    db.insert_event(conn, session_id, "comment", "u1", "Nick1", {"comment": "hi"}, {}, occurred_at="2026-01-01T00:00:10+00:00")
    db.insert_event(
        conn, session_id, "gift", "u2", "Gifter",
        {"diamond_count": 5, "repeat_count": 3, "streaking": False}, {},
        occurred_at="2026-01-01T00:00:20+00:00",
    )
    # A mid-streak tick that must NOT be double counted.
    db.insert_event(
        conn, session_id, "gift", "u2", "Gifter",
        {"diamond_count": 5, "repeat_count": 1, "streaking": True}, {},
        occurred_at="2026-01-01T00:00:19+00:00",
    )
    db.insert_event(conn, session_id, "follow", "u3", "Follower", {}, {}, occurred_at="2026-01-01T00:00:40+00:00")
    db.insert_event(conn, session_id, "room_enter", "u4", "Visitor", {}, {}, occurred_at="2026-01-01T00:00:50+00:00")

    db.end_session(conn, session_id, "manual")
    conn.execute(
        "UPDATE live_sessions SET started_at = ?, ended_at = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00", session_id),
    )
    conn.commit()

    result = aggregate_session_data(conn, session_id)
    stats = result["basic_stats"]

    assert stats["duration_seconds"] == 300
    assert stats["max_viewers"] == 200
    assert stats["avg_viewers"] == 150.0
    assert stats["final_likes"] == 1500
    assert stats["comment_count"] == 1
    assert stats["total_diamonds"] == 15  # 5*3 from the settled event only, not the streaking tick
    assert stats["unique_gifters"] == 1
    assert stats["follow_count"] == 1
    assert stats["unique_visitors"] == 1
    assert stats["battle_opponent_count"] == 0
    assert result["session"]["streamer_name"] == "Streamer One"
    assert result["screenshot_path"] is None


def test_gift_duplicate_delivery_is_not_double_counted():
    # Confirmed on real data: TikTok sometimes delivers the SAME settled
    # gift as several distinct webcast messages (same user/time/value, only
    # common.msg_id differs) that all share one log_id. Must count once.
    # log_id lives in payload (events.py promotes it there from raw_payload
    # at collection time -- see db.py's live_events comment for why).
    conn, session_id = make_session()
    for _ in range(4):
        db.insert_event(
            conn, session_id, "gift", "u1", "Gifter",
            {"diamond_count": 299, "repeat_count": 1, "streaking": False, "log_id": "shared-log-id-abc"},
            {},
            occurred_at="2026-01-01T00:00:00.500000+00:00",
        )
    result = aggregate_session_data(conn, session_id)
    assert result["basic_stats"]["total_diamonds"] == 299  # not 299*4


def test_gift_reused_log_id_with_different_values_are_both_counted():
    # Also confirmed on real data: TikTok can *reuse* the same log_id for
    # two genuinely separate gifts from the same user a few seconds apart.
    # A dedup keyed on log_id alone would wrongly collapse these and lose
    # one; grouping in diamond_count/repeat_count too keeps both.
    conn, session_id = make_session()
    db.insert_event(
        conn, session_id, "gift", "u1", "Gifter",
        {"diamond_count": 1, "repeat_count": 1, "streaking": False, "log_id": "reused-log-id-xyz"},
        {},
        occurred_at="2026-01-01T00:00:00+00:00",
    )
    db.insert_event(
        conn, session_id, "gift", "u1", "Gifter",
        {"diamond_count": 9, "repeat_count": 1, "streaking": False, "log_id": "reused-log-id-xyz"},
        {},
        occurred_at="2026-01-01T00:00:03+00:00",
    )
    result = aggregate_session_data(conn, session_id)
    assert result["basic_stats"]["total_diamonds"] == 10  # both counted, not just one


def test_gift_streak_ticks_sharing_log_id_count_only_the_final_settled_value():
    conn, session_id = make_session()
    for repeat_count in (1, 2, 3):
        db.insert_event(
            conn, session_id, "gift", "u1", "Gifter",
            {"diamond_count": 5, "repeat_count": repeat_count, "streaking": True, "log_id": "combo-log-id"},
            {},
            occurred_at=f"2026-01-01T00:00:0{repeat_count}+00:00",
        )
    db.insert_event(
        conn, session_id, "gift", "u1", "Gifter",
        {"diamond_count": 5, "repeat_count": 4, "streaking": False, "log_id": "combo-log-id"},
        {},
        occurred_at="2026-01-01T00:00:04+00:00",
    )
    result = aggregate_session_data(conn, session_id)
    assert result["basic_stats"]["total_diamonds"] == 20  # 5*4 from the settled tick only


def test_gift_missing_log_id_falls_back_to_row_id_without_collapsing_distinct_gifts():
    conn, session_id = make_session()
    for i in range(3):
        db.insert_event(
            conn, session_id, "gift", f"u{i}", f"Gifter{i}",
            {"diamond_count": 10, "repeat_count": 1, "streaking": False},  # no log_id at all
            {},
            occurred_at=f"2026-01-01T00:00:0{i}+00:00",
        )
    result = aggregate_session_data(conn, session_id)
    assert result["basic_stats"]["total_diamonds"] == 30  # all 3 counted independently


def test_comment_sampling_caps_and_preserves_order():
    conn, session_id = make_session()
    for i in range(10):
        db.insert_event(
            conn, session_id, "comment", f"u{i}", f"Nick{i}", {"comment": f"msg{i}"}, {},
            occurred_at=f"2026-01-01T00:00:{i:02d}+00:00",
        )

    result = aggregate_session_data(conn, session_id, max_comments=4)
    samples = result["comment_samples"]

    assert len(samples) == 4
    texts = [s["comment"] for s in samples]
    assert texts == sorted(texts, key=lambda t: int(t.replace("msg", "")))  # still chronological


def test_screenshot_path_returns_most_recent():
    conn, session_id = make_session()
    db.insert_screenshot(conn, session_id, "old.png", captured_at="2026-01-01T00:00:00+00:00")
    db.insert_screenshot(conn, session_id, "new.png", captured_at="2026-01-01T00:10:00+00:00")

    result = aggregate_session_data(conn, session_id)
    assert result["screenshot_path"] == "new.png"


def test_unknown_session_id_raises():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    try:
        aggregate_session_data(conn, 999)
        assert False, "expected ValueError"
    except ValueError:
        pass
