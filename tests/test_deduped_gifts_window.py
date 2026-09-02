"""report/data.py の deduped_gifts_subquery() の検証。

ギフト集計は streaking / log_id 重複 / log_id 再利用 の3つの罠があり、
その論理は dashboard/src/lib/queries.ts と対で維持されている。時間窓対応の
一般化がその論理を壊していないこと、および窓の境界で重複が二重計上され
ないことを確認する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.report import data


def make_session():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    streamer_id = db.get_or_create_streamer(conn, "streamer1", name="Streamer One")
    return conn, db.create_live_session(conn, streamer_id, title="s")


def gift(conn, session_id, at, diamonds, repeat=1, streaking=False, log_id="", user="u1"):
    db.insert_event(
        conn, session_id, "gift", user, "Gifter",
        {"diamond_count": diamonds, "repeat_count": repeat, "streaking": streaking, "log_id": log_id},
        {}, occurred_at=at,
    )


def total(conn, session_id, window=None, pad_sec=60):
    """window=None なら窓なし集計、window=(start,end) なら窓あり集計。"""
    if window is None:
        sql = f"SELECT SUM(diamond_value) FROM ({data.deduped_gifts_subquery()})"
        params = (session_id,)
    else:
        start, end = window
        from datetime import datetime, timedelta
        wide_start = (datetime.fromisoformat(start) - timedelta(seconds=pad_sec)).isoformat()
        wide_end = (datetime.fromisoformat(end) + timedelta(seconds=pad_sec)).isoformat()
        sql = f"SELECT SUM(diamond_value) FROM ({data.deduped_gifts_subquery(windowed=True)})"
        params = (session_id, wide_start, wide_end, start, end)
    row = conn.execute(sql, params).fetchone()
    return row[0] or 0


def test_unwindowed_sql_is_byte_identical_to_the_original_constant():
    # 既存の呼び出し(ダッシュボード/レポート)が従来と1文字も違わない
    # SQLを受け取ることを保証する -- 一般化の唯一の非交渉要件。
    assert data.deduped_gifts_subquery() == data._DEDUPED_GIFTS_SUBQUERY
    assert "occurred_at >= ?" not in data.deduped_gifts_subquery()


def test_streaking_ticks_are_not_double_counted_in_windowed_mode():
    conn, sid = make_session()
    # 同一コンボ: 途中経過(streaking=True)は 0 扱い、確定(False)だけ計上
    gift(conn, sid, "2026-01-01T10:00:10+00:00", 5, repeat=1, streaking=True, log_id="L1")
    gift(conn, sid, "2026-01-01T10:00:11+00:00", 5, repeat=2, streaking=True, log_id="L1")
    gift(conn, sid, "2026-01-01T10:00:12+00:00", 5, repeat=3, streaking=False, log_id="L1")

    expected = 15  # 5 * 3
    assert total(conn, sid) == expected
    assert total(conn, sid, ("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00")) == expected


def test_duplicate_delivery_of_the_same_gift_collapses_in_windowed_mode():
    conn, sid = make_session()
    # 同一ギフトが2メッセージで届く(log_id/値がすべて同じ)
    gift(conn, sid, "2026-01-01T10:00:12+00:00", 100, repeat=1, streaking=False, log_id="DUP")
    gift(conn, sid, "2026-01-01T10:00:12+00:00", 100, repeat=1, streaking=False, log_id="DUP")

    assert total(conn, sid) == 100
    assert total(conn, sid, ("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00")) == 100


def test_reused_log_id_with_different_values_are_both_counted_in_windowed_mode():
    conn, sid = make_session()
    # 同じ log_id が別ギフトに再利用されるケース -- 両方数える必要がある
    gift(conn, sid, "2026-01-01T10:00:12+00:00", 100, repeat=1, streaking=False, log_id="R")
    gift(conn, sid, "2026-01-01T10:00:18+00:00", 250, repeat=1, streaking=False, log_id="R")

    assert total(conn, sid) == 350
    assert total(conn, sid, ("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00")) == 350


def test_gifts_outside_the_window_are_excluded():
    conn, sid = make_session()
    gift(conn, sid, "2026-01-01T09:30:00+00:00", 10, log_id="A")   # 窓の前
    gift(conn, sid, "2026-01-01T10:30:00+00:00", 20, log_id="B")   # 窓の中
    gift(conn, sid, "2026-01-01T11:30:00+00:00", 40, log_id="C")   # 窓の後

    assert total(conn, sid) == 70
    assert total(conn, sid, ("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00")) == 20


def test_duplicate_straddling_the_window_boundary_is_not_double_counted():
    """窓境界の重複排除。内側の範囲を前後に広げていないと、同じギフトの
    重複が前後の窓に1回ずつ入り、合計が実際の2倍になる。"""
    conn, sid = make_session()
    # 確定イベントは窓内(10:00:05)だが、その重複が窓の直前(09:59:58)に届いた
    gift(conn, sid, "2026-01-01T09:59:58+00:00", 500, repeat=1, streaking=False, log_id="EDGE")
    gift(conn, sid, "2026-01-01T10:00:05+00:00", 500, repeat=1, streaking=False, log_id="EDGE")

    window = ("2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00")
    prev_window = ("2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")

    # 余裕60秒あり: 2件が1グループにまとまり、代表 occurred_at(=MAX)は
    # 10:00:05 なので当該窓に1回だけ計上され、前の窓には現れない。
    assert total(conn, sid, window, pad_sec=60) == 500
    assert total(conn, sid, prev_window, pad_sec=60) == 0

    # 余裕なし(pad=0)だと分断され、前後の窓で1回ずつ = 合計1000 になる。
    # 60秒の余裕が効いていることの対照確認。
    assert total(conn, sid, window, pad_sec=0) + total(conn, sid, prev_window, pad_sec=0) == 1000
