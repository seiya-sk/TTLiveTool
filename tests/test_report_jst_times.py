import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.report.data import aggregate_session_data


def make_session():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    streamer_id = db.get_or_create_streamer(conn, "streamer1", name="Streamer One")
    session_id = db.create_live_session(conn, streamer_id, title="test session")
    return conn, session_id


def test_comment_sample_time_is_converted_to_jst_and_rolls_over_midnight():
    # Everything is stored in UTC; the report prompt (and anything Claude
    # cites from it) must see JST instead, or a generated report ends up
    # quoting times 9 hours off from the streamer's actual local clock.
    # 15:20 UTC crosses into the next day in JST (00:20) -- a good canary
    # for a naive "just add 9 to the hour" bug that ignores date rollover.
    conn, session_id = make_session()
    db.insert_event(
        conn, session_id, "comment", "u1", "Nick1", {"comment": "hi"}, {},
        occurred_at="2026-08-24T15:20:13.562243+00:00",
    )

    result = aggregate_session_data(conn, session_id)
    samples = result["comment_samples"]

    assert len(samples) == 1
    assert samples[0]["time"] == "2026-08-25T00:20:13.562243+09:00"


def test_timeseries_minute_buckets_are_jst():
    conn, session_id = make_session()
    db.insert_event(
        conn, session_id, "viewer_count", None, None, {"viewer_count": 42}, {},
        occurred_at="2026-08-24T15:20:00+00:00",
    )

    result = aggregate_session_data(conn, session_id)
    minutes = [row["minute"] for row in result["timeseries"]]

    assert minutes == ["2026-08-25T00:20:00+09:00"]


def test_topic_bucket_keys_are_jst():
    conn, session_id = make_session()
    db.insert_event(
        conn, session_id, "comment", "u1", "Nick1", {"comment": "hi"}, {},
        occurred_at="2026-08-24T15:20:13+00:00",
    )

    result = aggregate_session_data(conn, session_id)
    buckets = result["comment_topic_buckets"]

    assert len(buckets) == 1
    # 5-minute bucket floor of 00:20 JST is still 00:20.
    assert buckets[0]["bucket"] == "2026-08-25T00:20:00+09:00"
