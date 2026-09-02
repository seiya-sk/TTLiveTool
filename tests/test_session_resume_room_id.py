"""room_id による同一ライブ判定(IP乗り換え / 再起動跨ぎの合算)の検証。

最重要なのは「繋がるべきものが繋がる」ことより「繋がってはいけないものが
繋がらない」こと -- 分割は後から繋ぎ直せるが、別ライブの誤結合はイベントが
混ざってしまい復元できない。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db

ROOM_A = "7680396023542991624"
ROOM_B = "7680395183259945746"
WINDOW = 45 * 60  # 本番と同じ45分


def fresh():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def ended_session(conn, streamer_id, room_id, end_type, ended_at_offset_sec=0):
    """終了済みセッションを1件作る。ended_at を現在から指定秒だけ過去にする。"""
    from datetime import datetime, timedelta, timezone

    sid = db.create_live_session(conn, streamer_id, room_id=room_id)
    ended_at = (datetime.now(timezone.utc) - timedelta(seconds=ended_at_offset_sec)).isoformat()
    conn.execute(
        "UPDATE live_sessions SET status='ended', ended_at=?, end_detection_type=? WHERE id=?",
        (ended_at, end_type, sid),
    )
    conn.commit()
    return sid


# --- 繋がるべきケース ------------------------------------------------------
def test_same_room_id_recently_auto_ended_is_resumable():
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    sid = ended_session(conn, s, ROOM_A, "auto", 60)
    assert db.find_resumable_session(conn, s, ROOM_A, WINDOW) == sid


def test_interrupted_session_is_resumable_across_restart():
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    sid = ended_session(conn, s, ROOM_A, "interrupted", 120)
    assert db.find_resumable_session(conn, s, ROOM_A, WINDOW) == sid


def test_recover_stale_marks_interrupted_and_is_then_resumable():
    """再起動時の補正 -> そのまま再開できること(ケースBの一気通貫)。"""
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    sid = db.create_live_session(conn, s, room_id=ROOM_A)
    # 明確に過去(1ヶ月前)にしておく -- "今日の日付" を使うと、実行時刻に
    # よっては未来時刻になり 45分窓に入ってしまいテストの意図が壊れる。
    db.insert_event(conn, sid, "comment", "u", "n", {"comment": "hi"}, {},
                    occurred_at="2026-08-01T10:00:00+00:00")

    stale = db.recover_stale_live_sessions(conn)
    assert stale == [sid]

    row = conn.execute(
        "SELECT status, end_detection_type, ended_at FROM live_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row[0] == "ended"          # status の値集合を増やさない
    assert row[1] == "interrupted"
    assert row[2] == "2026-08-01T10:00:00+00:00"  # 補正時刻ではなく最終イベント時刻

    # 45分窓の判定は ended_at 基準。1ヶ月前なので窓外。
    assert db.find_resumable_session(conn, s, ROOM_A, WINDOW) is None
    # 窓を十分広げれば再開対象として拾える
    assert db.find_resumable_session(conn, s, ROOM_A, 10**9) == sid


def test_resume_session_restores_live_and_keeps_started_at():
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    sid = ended_session(conn, s, ROOM_A, "auto", 30)
    before = conn.execute("SELECT started_at FROM live_sessions WHERE id=?", (sid,)).fetchone()[0]

    db.resume_session(conn, sid)

    row = conn.execute(
        "SELECT status, ended_at, end_detection_type, started_at FROM live_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row[0] == "live"
    assert row[1] is None
    assert row[2] is None
    assert row[3] == before          # 配信の実開始時刻であって再開時刻ではない


# --- 繋がってはいけないケース ----------------------------------------------
def test_different_room_id_is_not_resumable():
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    ended_session(conn, s, ROOM_B, "auto", 60)
    assert db.find_resumable_session(conn, s, ROOM_A, WINDOW) is None


def test_null_room_id_never_matches():
    """room_id が取れなかったときは繋がない -- 判定できない以上、
    別ライブを繋いでしまう危険を冒さない。"""
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    ended_session(conn, s, None, "auto", 60)
    assert db.find_resumable_session(conn, s, None, WINDOW) is None
    assert db.find_resumable_session(conn, s, "", WINDOW) is None


def test_other_streamer_same_room_id_is_not_resumable():
    conn = fresh()
    s1 = db.get_or_create_streamer(conn, "streamer1")
    s2 = db.get_or_create_streamer(conn, "streamer2")
    ended_session(conn, s1, ROOM_A, "auto", 60)
    assert db.find_resumable_session(conn, s2, ROOM_A, WINDOW) is None


def test_outside_the_time_window_is_not_resumable():
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    ended_session(conn, s, ROOM_A, "auto", WINDOW + 60)
    assert db.find_resumable_session(conn, s, ROOM_A, WINDOW) is None


def test_explicit_end_types_are_never_resumable():
    """TikTok が終了を明示した(live_end / normal_closure)、または人間が
    明示終了した(manual)セッションは、room_id が一致しても再開しない。"""
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    for end_type in ("live_end", "normal_closure", "manual"):
        conn.execute("DELETE FROM live_sessions")
        conn.commit()
        ended_session(conn, s, ROOM_A, end_type, 60)
        assert db.find_resumable_session(conn, s, ROOM_A, WINDOW) is None, end_type


def test_still_live_session_is_not_resumable():
    """まだ 'live' な行(ended_at IS NULL)は再開対象にしない -- 別スロットが
    録画中の可能性があり、二重書き込みになる。"""
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    db.create_live_session(conn, s, room_id=ROOM_A)
    assert db.find_resumable_session(conn, s, ROOM_A, WINDOW) is None


def test_most_recent_match_wins_when_several_qualify():
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    ended_session(conn, s, ROOM_A, "auto", 600)
    newer = ended_session(conn, s, ROOM_A, "auto", 60)
    assert db.find_resumable_session(conn, s, ROOM_A, WINDOW) == newer


def test_set_session_room_id_fills_only_when_empty():
    conn = fresh()
    s = db.get_or_create_streamer(conn, "streamer1")
    sid = db.create_live_session(conn, s)
    db.set_session_room_id(conn, sid, ROOM_A)
    assert conn.execute("SELECT room_id FROM live_sessions WHERE id=?", (sid,)).fetchone()[0] == ROOM_A
    db.set_session_room_id(conn, sid, ROOM_B)   # 上書きしない
    assert conn.execute("SELECT room_id FROM live_sessions WHERE id=?", (sid,)).fetchone()[0] == ROOM_A
