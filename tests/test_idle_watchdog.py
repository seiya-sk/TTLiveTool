import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.idle_watchdog import IdleWatchdog


def test_fires_after_timeout_with_no_events():
    async def scenario():
        async def on_timeout():
            return None

        watchdog = IdleWatchdog(timeout_sec=0.1, on_timeout=on_timeout)
        watchdog.start()
        await asyncio.sleep(0.3)
        return watchdog

    watchdog = asyncio.run(scenario())
    assert watchdog.fired


def test_notify_event_resets_the_timer():
    calls = []

    async def on_timeout():
        calls.append(1)

    async def scenario():
        watchdog = IdleWatchdog(timeout_sec=0.2, on_timeout=on_timeout)
        watchdog.start()
        # Keep "receiving events" faster than the timeout for longer than
        # the timeout itself would normally allow.
        for _ in range(4):
            await asyncio.sleep(0.1)
            watchdog.notify_event()
        assert not watchdog.fired
        watchdog.stop()

    asyncio.run(scenario())
    assert calls == []


def test_manual_stop_prevents_timeout_firing():
    calls = []

    async def on_timeout():
        calls.append(1)

    async def scenario():
        watchdog = IdleWatchdog(timeout_sec=0.1, on_timeout=on_timeout)
        watchdog.start()
        watchdog.stop()
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert calls == []
