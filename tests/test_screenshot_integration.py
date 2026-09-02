import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner
from tiktok_monitor.config import Settings


def make_runner(**settings_kwargs):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(
        username="some_streamer",
        db_path=":memory:",
        idle_timeout_sec=60,
         screenshot_delay_sec=0,  # 本番は600秒(開始10分後)。テストは _screenshot_task を await するので0にする
        **settings_kwargs,
    )
    runner = SessionRunner(conn, settings)
    return runner, conn


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=True)
def test_ensure_session_schedules_one_screenshot_and_records_it_on_success(mock_capture):
    async def scenario():
        runner, conn = make_runner()

        runner._ensure_session()
        session_id = runner.live_session_id
        assert runner._screenshot_task is not None
        await runner._screenshot_task

        mock_capture.assert_awaited_once()
        called_username = mock_capture.await_args.args[0]
        assert called_username == "some_streamer"

        rows = conn.execute(
            "SELECT live_session_id, image_path FROM live_screenshots"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == session_id
        assert rows[0][1]

        runner.manual_end()

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_no_screenshot_row_recorded_on_capture_failure(_mock_capture):
    async def scenario():
        runner, conn = make_runner()

        runner._ensure_session()
        await runner._screenshot_task

        rows = conn.execute("SELECT * FROM live_screenshots").fetchall()
        assert rows == []

        runner.manual_end()

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=True)
def test_reconnect_within_same_session_does_not_capture_again(mock_capture):
    async def scenario():
        runner, _conn = make_runner()

        runner._ensure_session()
        await runner._screenshot_task
        runner._ensure_session()  # simulates a reconnect handler calling _ensure_session again

        assert mock_capture.await_count == 1

        runner.manual_end()

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=True)
def test_screenshots_enabled_false_skips_capture_entirely(mock_capture):
    async def scenario():
        runner, conn = make_runner(screenshots_enabled=False)

        runner._ensure_session()

        assert runner._screenshot_task is None
        mock_capture.assert_not_awaited()
        rows = conn.execute("SELECT * FROM live_screenshots").fetchall()
        assert rows == []

        runner.manual_end()

    asyncio.run(scenario())


@patch(
    "tiktok_monitor.client.screenshot_module.capture_live_screenshot",
    new_callable=AsyncMock,
    side_effect=RuntimeError("boom"),
)
def test_capture_screenshot_exception_does_not_propagate(_mock_capture):
    async def scenario():
        runner, conn = make_runner()

        runner._ensure_session()
        await runner._screenshot_task  # must not raise

        rows = conn.execute("SELECT * FROM live_screenshots").fetchall()
        assert rows == []

        runner.manual_end()

    asyncio.run(scenario())
