"""SIGINT で収集プロセスを停止 → 再起動 → 同じライブを継続、の一気通貫検証。

2026-09-01 に実際に抜けていたテスト。それまで再起動跨ぎの継続は
`interrupted`(クラッシュ経路)でしか検証しておらず、**実際の運用手順で
ある SIGINT による正常停止経路**が一度もテストされていなかった。その結果、
停止時に 'manual' で閉じられて再開対象から外れ、再起動のたびに同じライブが
別セッションに割れていた(mu_chan38 id=36→49 など5件)。
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor import proxy_pool_trial as ppt
from tiktok_monitor.client import SessionRunner
from tiktok_monitor.config import Settings

ROOM = "7680445481654225671"
WINDOW = 45 * 60


def make_runner(conn, room_id=ROOM, username="mu_chan38"):
    settings = Settings(
        username=username, db_path=":memory:", idle_timeout_sec=60,
        screenshot_delay_sec=0, screenshots_enabled=False,
    )
    runner = SessionRunner(conn, settings)
    runner.client = SimpleNamespace(room_id=room_id)
    return runner


def comment(runner, text="hi"):
    runner._record_event(
        lambda _e: ("comment", "u1", "Nick", {"comment": text}),
        SimpleNamespace(common=None),
    )


def sessions(conn):
    return conn.execute(
        "SELECT id, status, end_detection_type, room_id FROM live_sessions ORDER BY id"
    ).fetchall()


def test_sigint_stop_then_restart_continues_the_same_session():
    """**実際の運用手順そのもの。** 稼働中 → SIGINT 停止 → 再起動 →
    同じ room_id にまた繋がる → 同じセッションに書き戻される。"""
    async def scenario():
        conn = db.connect(":memory:")
        db.init_schema(conn)

        # --- 稼働中 ---
        runner1 = make_runner(conn)
        comment(runner1, "停止前")
        sid = runner1.live_session_id
        assert sid is not None

        # --- SIGINT による停止(proxy_pool_trial の停止経路) ---
        runner1.interrupted_end()
        row = conn.execute(
            "SELECT status, end_detection_type FROM live_sessions WHERE id=?", (sid,)
        ).fetchone()
        assert row == ("ended", "interrupted"), f"停止時の終了種別が想定外: {row}"

        # --- 再起動: 新しいプロセス = 新しい SessionRunner ---
        runner2 = make_runner(conn)
        comment(runner2, "再起動後")

        assert runner2.live_session_id == sid, "再起動後に同じセッションへ継続していない"
        rows = sessions(conn)
        assert len(rows) == 1, f"セッションが分割された: {rows}"
        assert rows[0][1] == "live"
        assert rows[0][2] is None          # 再開したので終了種別はクリア
        assert conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0] == 2

    asyncio.run(scenario())


def test_manual_end_still_does_not_resume():
    """人が明示的に終了した(watch.py / main.py の Ctrl+C)場合は、
    従来どおり再開しない。今回の変更でそこまで緩めていないことの確認。"""
    async def scenario():
        conn = db.connect(":memory:")
        db.init_schema(conn)
        runner1 = make_runner(conn)
        comment(runner1)
        sid = runner1.live_session_id
        runner1.manual_end()
        assert conn.execute(
            "SELECT end_detection_type FROM live_sessions WHERE id=?", (sid,)
        ).fetchone()[0] == "manual"

        runner2 = make_runner(conn)
        comment(runner2)
        assert runner2.live_session_id != sid
        assert len(sessions(conn)) == 2      # 分かれるのが正しい

    asyncio.run(scenario())


def test_restart_after_a_live_end_does_not_resume():
    """TikTok が終了を通知したセッションは、再起動しても再開しない。
    終わった配信に次の配信のイベントを書き込む汚染を防ぐ。"""
    async def scenario():
        conn = db.connect(":memory:")
        db.init_schema(conn)
        runner1 = make_runner(conn)
        comment(runner1)
        sid = runner1.live_session_id
        runner1.end_now("live_end")

        runner2 = make_runner(conn, room_id=ROOM)   # 同じ room_id でも
        comment(runner2)
        assert runner2.live_session_id != sid
        assert len(sessions(conn)) == 2

    asyncio.run(scenario())


def test_restart_with_a_different_room_id_starts_a_new_session():
    """再起動後に別の配信(新 room_id)が始まっていたら、別セッションになる。"""
    async def scenario():
        conn = db.connect(":memory:")
        db.init_schema(conn)
        runner1 = make_runner(conn, room_id=ROOM)
        comment(runner1)
        sid = runner1.live_session_id
        runner1.interrupted_end()

        runner2 = make_runner(conn, room_id="9999999999999999999")
        comment(runner2)
        assert runner2.live_session_id != sid
        rows = sessions(conn)
        assert len(rows) == 2
        assert rows[0][3] != rows[1][3]      # room_id が違う

    asyncio.run(scenario())


def test_interrupted_end_clears_the_screenshot_task():
    """停止時に、まだ待機中のスクショタスクを取り消すこと。
    残すと停止後に撮影が走り、閉じたセッションに紐づく。"""
    async def scenario():
        conn = db.connect(":memory:")
        db.init_schema(conn)
        runner = make_runner(conn)
        comment(runner)
        runner._screenshot_task = asyncio.get_event_loop().create_task(asyncio.sleep(60))
        runner.interrupted_end()
        await asyncio.sleep(0)
        assert runner._screenshot_task.cancelled()

    asyncio.run(scenario())


def test_trial_shutdown_path_uses_interrupted_not_manual():
    """proxy_pool_trial の停止経路が interrupted_end() を呼ぶこと
    (コードが manual_end() に戻ってしまう回帰を防ぐ)。"""
    import inspect
    src = inspect.getsource(ppt)
    assert "runner.interrupted_end()" in src
    assert "slot.runner.interrupted_end()" in src
    assert "runner.manual_end()" not in src, "停止経路が manual_end() に戻っている"
