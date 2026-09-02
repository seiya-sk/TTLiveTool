import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner
from tiktok_monitor.config import Settings


def make_runner():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(
        username="my_own_handle", db_path=":memory:", idle_timeout_sec=60,
        screenshot_delay_sec=0,  # 本番は600秒(開始10分後)。テストは _screenshot_task を await するので0にする
    )
    runner = SessionRunner(conn, settings)
    return runner, conn


class FakeBattleEvent:
    """Shaped like a real LinkMicArmiesEvent well enough to exercise the
    reflection-based serializer and regex extraction end-to-end, without
    depending on catching an actual live PK battle."""

    def __init__(self):
        self.common = None
        self.battle_id = 999
        self.armies = {
            1: SimpleNamespace(user=SimpleNamespace(display_id="opponent_streamer", nickname="Opponent")),
            2: SimpleNamespace(user=SimpleNamespace(display_id="my_own_handle", nickname="Me")),
        }


async def _finish(runner):
    """These tests aren't about screenshots; capture_live_screenshot is
    mocked out (avoids launching a real headless browser in the test suite)
    and its fire-and-forget task is awaited here so it can't outlive the
    event loop and log a stray 'Task was destroyed' warning."""
    if runner._screenshot_task is not None:
        await runner._screenshot_task
    runner.manual_end()


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_handle_battle_event_inserts_row_and_dedupes(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        event = FakeBattleEvent()

        runner._handle_battle_event(event)
        runner._handle_battle_event(event)  # same opponent again -> should not duplicate

        rows = conn.execute(
            "SELECT event_type, user_id, payload FROM live_events WHERE event_type='battle_opponent'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "battle_opponent"
        assert rows[0][1] == "opponent_streamer"
        assert "FakeBattleEvent" in rows[0][2]
        await _finish(runner)

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_handle_battle_event_excludes_own_handle(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        event = FakeBattleEvent()

        runner._handle_battle_event(event)

        user_ids = [
            row[0]
            for row in conn.execute(
                "SELECT user_id FROM live_events WHERE event_type='battle_opponent'"
            ).fetchall()
        ]
        assert "my_own_handle" not in user_ids
        await _finish(runner)

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_handle_battle_event_starts_a_session_if_none_active(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        assert runner.live_session_id is None

        runner._handle_battle_event(FakeBattleEvent())

        assert runner.live_session_id is not None
        status = conn.execute(
            "SELECT status FROM live_sessions WHERE id=?", (runner.live_session_id,)
        ).fetchone()[0]
        assert status == "live"
        await _finish(runner)

    asyncio.run(scenario())
