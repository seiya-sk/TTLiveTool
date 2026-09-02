"""終了処理中の遅延イベントが「幻のセッション」を作らないことの検証。

最重要なのは「幻を作らない」ことより **「本物を取りこぼさない」** こと。
幻は後から除去できるが、取りこぼした配信のデータは二度と取れない。
そのため、遅延イベントを捨てる判定が runner インスタンスの _ended だけに
依存し、room_id や経過時間の推測を使っていないことを固定する。
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner
from tiktok_monitor.config import Settings

ROOM = "7680453353676294933"


def make_runner(conn=None, room_id=ROOM):
    conn = conn or db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(
        username="lllleeee444",
        db_path=":memory:",
        idle_timeout_sec=60,
        screenshot_delay_sec=0,
        screenshots_enabled=False,
    )
    runner = SessionRunner(conn, settings)
    # current_room_id() は self.client 越しに読むので、最小の偽物を置く
    runner.client = SimpleNamespace(room_id=room_id)
    return runner, conn


def comment(runner, text="hi"):
    """通常のコメント受信と同じ経路を通す。"""
    runner._record_event(
        lambda _e: ("comment", "u1", "Nick", {"comment": text}),
        SimpleNamespace(common=None),
    )


def sessions(conn):
    return conn.execute("SELECT id, status, end_detection_type FROM live_sessions ORDER BY id").fetchall()


# --- 幻を作らない ----------------------------------------------------------
def test_late_event_after_live_end_does_not_create_a_new_session():
    """LiveEnd で終了した直後、切断処理中に届いた遅延イベントは捨てる。
    2026-09-01 の session 46(イベント2件、どちらもセッション作成時刻より
    前)を再現する回帰テスト。"""
    async def scenario():
        runner, conn = make_runner()
        comment(runner)                       # 通常の受信 -> セッション1が出来る
        assert len(sessions(conn)) == 1

        runner.end_now("live_end")            # TikTok が終了を通知
        comment(runner, "遅延イベント")        # 切断処理中に遅れて届いた

        rows = sessions(conn)
        assert len(rows) == 1, f"幻のセッションが作られた: {rows}"
        assert rows[0][1] == "ended"
        assert rows[0][2] == "live_end"

    asyncio.run(scenario())

def test_late_events_of_every_kind_are_dropped_after_end():
    """コメントだけでなく、バトル・宝箱の経路も同じガードを通ること。"""
    async def scenario():
        runner, conn = make_runner()
        comment(runner)
        runner.end_now("normal_closure")

        runner._handle_battle_event(SimpleNamespace(common=None, battle_id=1, armies={}))
        runner._handle_envelope_event(SimpleNamespace(common=None))
        comment(runner, "late")

        assert len(sessions(conn)) == 1

    asyncio.run(scenario())

def test_end_now_is_a_noop_when_no_session_was_ever_started():
    """セッションが無い状態の end_now() は何もしない(= _ended も立たない)。
    そのため、その後に届いたイベントは通常どおり記録される -- 「まだ何も
    始まっていない」のを「終了済み」と誤認して取りこぼさないことの確認。"""
    async def scenario():
        runner, conn = make_runner()
        runner.end_now("live_end")
        assert runner._ended is False

        comment(runner, "本物")
        rows = sessions(conn)
        assert len(rows) == 1 and rows[0][1] == "live"

    asyncio.run(scenario())

def test_no_crash_when_a_late_event_arrives_with_no_session():
    """_ended が立っていて live_session_id も None の状態で遅延イベントが
    届いても落ちないこと。ガードが無いと insert_event が None を渡されて
    例外になり、上位の再接続ロジックまで伝播する。"""
    async def scenario():
        runner, conn = make_runner()
        runner._ended = True                  # end_now 後と同じ状態
        comment(runner, "late")               # 例外が出ないこと
        assert sessions(conn) == []

    asyncio.run(scenario())

# --- 本物を取りこぼさない(最重要) -----------------------------------------
def test_a_fresh_runner_records_normally_after_a_previous_one_ended():
    """**本物の新配信/再開を取りこぼさないことの証明。**

    前の runner が live_end で終わっていても、プールが新しい録画タスクを
    起こせば新しい SessionRunner(_ended=False)が処理する。ガードは
    インスタンス単位なので、新しい runner は一切影響を受けない。"""
    async def scenario():
        runner1, conn = make_runner()
        comment(runner1)
        runner1.end_now("live_end")
        comment(runner1, "late")              # 捨てられる

        # プールが同じライバーを再び録画開始したときと同じ状況
        runner2 = SessionRunner(conn, runner1.settings)
        runner2.client = SimpleNamespace(room_id="9999999999999999999")  # 新しい配信=新room_id
        comment(runner2, "本物のコメント")

        rows = sessions(conn)
        assert len(rows) == 2, "本物の新配信が記録されていない"
        assert rows[1][1] == "live"
        n = conn.execute("SELECT COUNT(*) FROM live_events WHERE live_session_id=?", (rows[1][0],)).fetchone()[0]
        assert n == 1

    asyncio.run(scenario())

def test_a_fresh_runner_still_resumes_the_same_room_id():
    """同じ room_id の継続(IP乗り換え/再起動跨ぎ)も従来どおり働くこと。
    ガードが再開経路を塞いでいないことの確認。"""
    async def scenario():
        runner1, conn = make_runner()
        comment(runner1)
        sid = runner1.live_session_id
        # 決定的な終了シグナルではなく「録画が止まった」扱いで閉じる
        conn.execute(
            "UPDATE live_sessions SET status='ended', ended_at=?, end_detection_type='interrupted' WHERE id=?",
            (db.utc_now_iso(), sid),
        )
        conn.commit()

        runner2 = SessionRunner(conn, runner1.settings)
        runner2.client = SimpleNamespace(room_id=ROOM)   # 同じ配信
        comment(runner2, "継続")

        assert runner2.live_session_id == sid, "同じ room_id なのに再開されなかった"
        assert len(sessions(conn)) == 1
        assert sessions(conn)[0][1] == "live"

    asyncio.run(scenario())

def test_guard_does_not_depend_on_room_id_or_elapsed_time():
    """ガードが _ended だけを見ていること。room_id が同じでも、終了して
    いない runner は普通にセッションを作る(推測で弾いていない証拠)。"""
    async def scenario():
        runner, conn = make_runner()
        assert runner._ended is False
        comment(runner)
        assert runner.live_session_id is not None
        first = runner.live_session_id

        # 同じ room_id のまま、まだ終了していないので記録を続ける
        comment(runner, "continues")
        assert runner.live_session_id == first
        assert conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0] == 2

    asyncio.run(scenario())
