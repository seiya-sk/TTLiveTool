import asyncio
import time
from typing import Awaitable, Callable


class IdleWatchdog:
    """Watches time since the last received event; fires on_timeout once no
    event has arrived for `timeout_sec`, signaling the caller to treat the
    live as ended (auto-detection per design doc section 2)."""

    def __init__(self, timeout_sec: float, on_timeout: Callable[[], Awaitable[None]]):
        self._timeout_sec = timeout_sec
        self._on_timeout = on_timeout
        self._last_event_at = time.monotonic()
        self._task: asyncio.Task | None = None
        self._fired = False

    def notify_event(self) -> None:
        self._last_event_at = time.monotonic()

    async def _run(self) -> None:
        while not self._fired:
            elapsed = time.monotonic() - self._last_event_at
            remaining = self._timeout_sec - elapsed
            if remaining <= 0:
                self._fired = True
                await self._on_timeout()
                return
            await asyncio.sleep(min(remaining, 5.0))

    def start(self) -> None:
        self._last_event_at = time.monotonic()
        self._fired = False
        self._task = asyncio.get_event_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def fired(self) -> bool:
        return self._fired
