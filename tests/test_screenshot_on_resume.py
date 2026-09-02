"""再開したセッションでもスクリーンショットを撮る(A-2 の回帰テスト)。

2026-09-02 の棚卸しで見つかった欠損:
  セッション78-90 の13本(15分超は12本)が1枚も撮れていなかった。
  原因は _ensure_session() の再開分岐が、スクショ/アバターのスケジュール
  より前に return していたこと。再起動や乗り換えを挟むのは長時間配信ほど
  多いので、**価値の高い配信ほど撮れない**という逆の偏りが出ていた。

ログには1行も出ない欠損だった(撮影を試みないので失敗ログも出ない)。
だからこそテストで固定する。
"""
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner
from tiktok_monitor.config import Settings


def make_runner(tmp_path, delay=600.0, screenshots_enabled=True):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(
        username="someone", db_path=":memory:", idle_timeout_sec=60,
        screenshot_delay_sec=delay, screenshots_enabled=screenshots_enabled,
        screenshot_dir=str(tmp_path),
    )
    runner = SessionRunner(conn, settings)
    runner.streamer_id = db.get_or_create_streamer(conn, "someone")
    return runner, conn


def make_session(conn, streamer_id, room_id="ROOM_A", started_minutes_ago=0):
    started = (datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)).isoformat()
    sid = db.create_live_session(conn, streamer_id, room_id=room_id)
    conn.execute("UPDATE live_sessions SET started_at = ? WHERE id = ?", (started, sid))
    conn.commit()
    return sid


def resume_into(runner, session_id):
    """プールが再開先を指定して繋ぎ直したのと同じ状態を作る。"""
    runner.resume_session_id = session_id
    runner.current_room_id = lambda: "ROOM_A"


# --- 1. 再開でもスケジュールされること ------------------------------------
def test_resumed_session_still_schedules_a_screenshot(tmp_path):
    runner, conn = make_runner(tmp_path)
    sid = make_session(conn, runner.streamer_id)
    db.end_session(conn, sid, "interrupted")
    resume_into(runner, sid)

    async def scenario():
        runner._ensure_session()
        assert runner.live_session_id == sid, "再開されていない(テストの前提が崩れた)"
        assert runner._screenshot_task is not None, \
            "再開したセッションでスクショが仕掛けられていない"
        assert runner._avatar_task is not None, \
            "再開したセッションでアバター取得が仕掛けられていない"
        runner._screenshot_task.cancel()
        runner._avatar_task.cancel()

    asyncio.run(scenario())


def test_new_session_still_schedules_a_screenshot(tmp_path):
    """共通化で新規作成側を壊していないこと。"""
    runner, conn = make_runner(tmp_path)
    runner.current_room_id = lambda: "ROOM_NEW"

    async def scenario():
        runner._ensure_session()
        assert runner._screenshot_task is not None
        runner._screenshot_task.cancel()
        runner._avatar_task.cancel()

    asyncio.run(scenario())


def test_watchdog_starts_before_the_tasks_are_scheduled(tmp_path):
    """_start_watchdog() の順序を変えていないこと(明示の要件)。"""
    runner, conn = make_runner(tmp_path)
    sid = make_session(conn, runner.streamer_id)
    db.end_session(conn, sid, "interrupted")
    resume_into(runner, sid)
    order = []
    runner._start_watchdog = lambda: order.append("watchdog")
    runner._schedule_start_tasks = lambda: order.append("tasks")

    runner._ensure_session()

    assert order == ["watchdog", "tasks"], f"順序が変わっている: {order}"


# --- 2. 多重起動しないこと -------------------------------------------------
def test_repeated_resume_does_not_stack_screenshot_tasks(tmp_path):
    """同じランナーで再開が複数回起きてもタスクは1つ。"""
    runner, conn = make_runner(tmp_path)
    sid = make_session(conn, runner.streamer_id)

    async def scenario():
        runner.live_session_id = sid
        runner._schedule_start_tasks()
        first = runner._screenshot_task
        runner._schedule_start_tasks()
        runner._schedule_start_tasks()
        assert runner._screenshot_task is first, "スクショタスクが作り直された"
        first.cancel()
        runner._avatar_task.cancel()

    asyncio.run(scenario())


def test_a_session_that_already_has_a_screenshot_is_not_shot_again(tmp_path):
    """プロセスをまたぐ多重の防止。メモリ上のフラグでは越えられないので
    live_screenshots を見る(一意制約が無いため、見ないと複数枚入る)。"""
    runner, conn = make_runner(tmp_path, delay=0.0)
    sid = make_session(conn, runner.streamer_id)
    db.insert_screenshot(conn, sid, "already/there.png")
    captured = []
    runner._capture_screenshot = AsyncMock(side_effect=lambda i: captured.append(i))

    asyncio.run(runner._capture_screenshot_later(sid))

    assert captured == [], "既に1枚あるのにもう1枚撮った"
    assert conn.execute("SELECT COUNT(*) FROM live_screenshots").fetchone()[0] == 1


# --- 3. 待ち時間はセッション開始からの経過で決める --------------------------
def test_wait_is_measured_from_session_start_not_from_scheduling(tmp_path):
    """再開時点で既に10分過ぎていれば待たない。

    ここが「仕掛けた時刻から10分」だと、10分より短い間隔で再起動や
    乗り換えが起きる配信はいつまでも撮れない。
    """
    runner, conn = make_runner(tmp_path, delay=600.0)
    sid = make_session(conn, runner.streamer_id, started_minutes_ago=42)

    assert runner._screenshot_wait_sec(sid) == 0.0, \
        "開始から42分経っているのに、まだ待とうとしている"


def test_partial_wait_when_resumed_before_the_delay_elapses(tmp_path):
    """まだ10分経っていなければ、残り時間だけ待つ(10分丸ごとではない)。"""
    runner, conn = make_runner(tmp_path, delay=600.0)
    sid = make_session(conn, runner.streamer_id, started_minutes_ago=4)

    wait = runner._screenshot_wait_sec(sid)

    assert 300 < wait <= 360, f"残り時間になっていない: {wait}"
    assert wait < 600, "再開のたびに10分待ち直している"


def test_overdue_session_is_captured_immediately(tmp_path):
    """経過済みなら即撮る(待たない)ことを、実際にタスクを走らせて確認。"""
    runner, conn = make_runner(tmp_path, delay=600.0)
    sid = make_session(conn, runner.streamer_id, started_minutes_ago=30)
    captured = []
    runner._capture_screenshot = AsyncMock(side_effect=lambda i: captured.append(i))

    async def scenario():
        t0 = time.monotonic()
        await asyncio.wait_for(runner._capture_screenshot_later(sid), timeout=5)
        return time.monotonic() - t0

    elapsed = asyncio.run(scenario())
    assert captured == [sid], "経過済みなのに撮っていない"
    assert elapsed < 1.0, f"待ってしまっている: {elapsed:.1f}s"


def test_unparsable_started_at_falls_back_to_the_full_delay(tmp_path):
    """開始時刻が読めないときは、待たずに撮るのではなく従来どおり待つ。
    開始直後は準備画面や暗転が写るため、撮り急ぐほうが害が大きい。"""
    runner, conn = make_runner(tmp_path, delay=600.0)
    sid = make_session(conn, runner.streamer_id)
    conn.execute("UPDATE live_sessions SET started_at = 'not-a-date' WHERE id = ?", (sid,))
    conn.commit()

    assert runner._screenshot_wait_sec(sid) == 600.0


# --- 4. 無効化フラグ -------------------------------------------------------
def test_screenshots_disabled_still_skips(tmp_path):
    """TTS_DISABLE_SCREENSHOTS 相当。無効なら再開でも仕掛けない。"""
    runner, conn = make_runner(tmp_path, screenshots_enabled=False)
    sid = make_session(conn, runner.streamer_id)

    async def scenario():
        runner.live_session_id = sid
        runner._schedule_start_tasks()
        assert runner._screenshot_task is None
        runner._avatar_task.cancel()

    asyncio.run(scenario())


def test_run_trial_sh_unsets_the_disable_flag():
    """起動スクリプトが変数を明示的に消していること。

    「設定しない」だけでは足りない -- 起動元シェルに残っていると黙って
    無効になり、ログには何も出ない。実際に約10時間の欠損を生んだ。
    """
    script = (Path(__file__).resolve().parents[1] / "run_trial.sh").read_text(encoding="utf-8")
    assert "unset TTS_DISABLE_SCREENSHOTS" in script
