import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner
from tiktok_monitor.config import Settings
from tiktok_monitor.events import normalize_gift

# Exercises the full real-time recording path (SessionRunner._record_event
# -> normalize_gift -> db.insert_event) end-to-end against the post-split
# schema, standing in for "start watch.py and record a live gift" without
# needing an actual TikTok connection -- the concern being verified is that
# a freshly recorded gift event lands log_id in payload (not raw_payload)
# and its full raw_payload lands in live_event_raw_payloads, unbroken by
# the raw_payload table split.


class FakeGiftEvent:
    def __init__(self, log_id: str, diamond_count: int = 5, repeat_count: int = 1, streaking: bool = False):
        self.common = None
        self.user = SimpleNamespace(unique_id="gifter1", nickname="Gifter")
        self.gift = SimpleNamespace(id=1, name="Rose", type=1, diamond_count=diamond_count)
        self.repeat_count = repeat_count
        self.repeat_end = True
        self.streaking = streaking
        self.value = 0.05
        self.log_id = log_id


def make_runner():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(
        username="my_own_handle", db_path=":memory:", idle_timeout_sec=60,
        screenshot_delay_sec=0,  # 本番は600秒(開始10分後)。テストは _screenshot_task を await するので0にする
    )
    runner = SessionRunner(conn, settings)
    return runner, conn


async def _finish(runner):
    if runner._screenshot_task is not None:
        await runner._screenshot_task
    runner.manual_end()


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_recorded_gift_event_lands_log_id_in_payload_and_raw_payload_in_new_table(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        event = FakeGiftEvent(log_id="live-log-id-1")

        runner._record_event(normalize_gift, event)

        row = conn.execute(
            "SELECT id, payload FROM live_events WHERE event_type='gift'"
        ).fetchone()
        assert row is not None
        event_id, payload_json = row
        assert '"log_id": "live-log-id-1"' in payload_json or '"log_id":"live-log-id-1"' in payload_json

        raw = db.get_raw_payload(conn, event_id)
        assert raw is not None
        assert raw["log_id"] == "live-log-id-1"  # full original event preserved, not just the promoted field

        await _finish(runner)

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_recorded_duplicate_gift_deliveries_are_deduped_by_promoted_log_id(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        for _ in range(3):
            runner._record_event(normalize_gift, FakeGiftEvent(log_id="dup-log-id", diamond_count=10))

        count = conn.execute("SELECT COUNT(*) FROM live_events WHERE event_type='gift'").fetchone()[0]
        assert count == 3  # each delivery still recorded as its own row...

        from tiktok_monitor.report.data import _DEDUPED_GIFTS_SUBQUERY

        total = conn.execute(
            f"SELECT SUM(diamond_value) FROM ({_DEDUPED_GIFTS_SUBQUERY})", (runner.live_session_id,)
        ).fetchone()[0]
        assert total == 10  # ...but the dedup query (now keyed on payload.log_id) still collapses them to one

        await _finish(runner)

    asyncio.run(scenario())
