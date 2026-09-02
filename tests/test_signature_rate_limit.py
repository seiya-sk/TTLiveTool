import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from TikTokLive.client.errors import SignatureRateLimitError
from TikTokLive import TikTokLiveClient

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner, _attach_sign_rate_limit_logger, run_with_reconnect
from tiktok_monitor.config import Settings

# Phase 5 measurement needs to tell "TikTok blocked us" apart from "Euler
# Stream's signing quota ran out" -- both used to land in the same generic
# "connection error" log line. These tests exercise run_with_reconnect's
# error-branch dispatch directly (not client.start()/a real connection).


class _StopLoop(Exception):
    """Raised from the mocked asyncio.sleep to break run_with_reconnect's
    while-loop after the interesting part of one iteration has run."""


def make_runner():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(username="target_streamer", db_path=":memory:", idle_timeout_sec=60)
    runner = SessionRunner(conn, settings)
    return runner, conn


def make_signature_rate_limit_error(remaining="5", reset="1893456000"):
    """Builds a real SignatureRateLimitError the same way TikTokLive's own
    fetch_signed_websocket.py does, off a fake httpx.Response carrying the
    RateLimit-Remaining/RateLimit-Reset headers -- not a hand-rolled stub,
    so this stays honest to what retry_after/reset_time actually compute."""
    fake_response = MagicMock()
    fake_response.headers = {"RateLimit-Remaining": remaining, "RateLimit-Reset": reset}
    return SignatureRateLimitError(
        None, "Too many connections started, try again in %s seconds.", response=fake_response
    )


@patch.object(SessionRunner, "build_client")
def test_signature_rate_limit_error_is_logged_distinctly(mock_build_client, caplog):
    async def scenario():
        runner, _conn = make_runner()

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=make_signature_rate_limit_error(remaining="5", reset="1893456000"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client

        with patch("asyncio.sleep", AsyncMock(side_effect=_StopLoop)):
            with caplog.at_level("WARNING", logger="tiktok_monitor.client"):
                try:
                    await run_with_reconnect(runner, runner.settings)
                except _StopLoop:
                    pass

        runner.manual_end()

    asyncio.run(scenario())

    messages = [r.message for r in caplog.records]
    assert any("rate limit" in m.lower() for m in messages)
    assert any("Euler Stream" in m for m in messages)
    # retry_after (from TikTokLive 7.0.0's SignatureRateLimitError, which
    # actually returns the RateLimit-Remaining header value, not a real
    # duration -- see run_with_reconnect's SignatureRateLimitError branch)
    # is still logged for visibility, but must not be presented as a wait
    # time; the real wait duration is logged separately as wait=...
    assert any("retry_after=5" in m for m in messages)
    assert any("wait=" in m for m in messages)
    assert any("2030-01-01" in m for m in messages)  # reset_at ISO timestamp derived from reset=1893456000 (epoch)
    # Must NOT also fall into the generic connection-error branch.
    assert not any("connection error" in m for m in messages)


# --- reset_time epoch-vs-countdown handling (the "kana18724 incident") ----
#
# Real production data showed retry_after stuck at a constant 0 (it's
# actually TikTokLive 7.0.0's RateLimit-Remaining header, not a duration --
# see run_with_reconnect) while reset_time counted DOWN by ~1 real second
# per elapsed second across repeated polls (14879 -> 14816 -> ... over
# ~63s), which a fixed Unix epoch value cannot do. These tests lock in
# treating a reset_time that isn't plausibly "now" (i.e. too small to be a
# real current epoch timestamp) as a countdown-from-now instead.


@patch.object(SessionRunner, "build_client")
def test_reset_time_as_a_countdown_sleeps_the_remaining_seconds_not_retry_after(mock_build_client):
    async def scenario():
        runner, _conn = make_runner()

        fake_client = MagicMock()
        # remaining="0" -> retry_after computes to 0, exactly as observed
        # during the incident once the quota was fully exhausted.
        fake_client.start = AsyncMock(side_effect=make_signature_rate_limit_error(remaining="0", reset="120"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client

        sleep_durations = []

        async def fake_sleep(seconds):
            sleep_durations.append(seconds)
            raise _StopLoop()

        with patch("asyncio.sleep", fake_sleep):
            try:
                await run_with_reconnect(runner, runner.settings)
            except _StopLoop:
                pass

        runner.manual_end()
        return sleep_durations

    sleep_durations = asyncio.run(scenario())

    # Must sleep ~120s (the countdown), not 1s (what max(retry_after=0, 1.0)
    # would have produced -- hammering an exhausted quota every second).
    assert sleep_durations == [120]


@patch.object(SessionRunner, "build_client")
def test_reset_time_as_a_countdown_is_capped_at_max_sleep_sec(mock_build_client):
    from tiktok_monitor.client import MAX_SLEEP_SEC

    async def scenario():
        runner, _conn = make_runner()

        fake_client = MagicMock()
        # 14879s (~4h08m) matches the real account_day incident value --
        # far longer than any single sleep should run unattended.
        fake_client.start = AsyncMock(side_effect=make_signature_rate_limit_error(remaining="0", reset="14879"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client

        sleep_durations = []

        async def fake_sleep(seconds):
            sleep_durations.append(seconds)
            raise _StopLoop()

        with patch("asyncio.sleep", fake_sleep):
            try:
                await run_with_reconnect(runner, runner.settings)
            except _StopLoop:
                pass

        runner.manual_end()
        return sleep_durations

    sleep_durations = asyncio.run(scenario())

    assert sleep_durations == [MAX_SLEEP_SEC]


@patch.object(SessionRunner, "build_client")
def test_reset_time_as_a_plausible_epoch_sleeps_until_that_moment(mock_build_client):
    async def scenario():
        runner, _conn = make_runner()

        fake_client = MagicMock()
        # A value only a genuine future Unix timestamp could produce
        # (year 2030) -- must be interpreted as an absolute moment, not
        # 1893456000 seconds (~60 years) of countdown.
        fake_client.start = AsyncMock(side_effect=make_signature_rate_limit_error(remaining="0", reset="1893456000"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client

        sleep_durations = []

        async def fake_sleep(seconds):
            sleep_durations.append(seconds)
            raise _StopLoop()

        with patch("asyncio.sleep", fake_sleep):
            try:
                await run_with_reconnect(runner, runner.settings)
            except _StopLoop:
                pass

        runner.manual_end()
        return sleep_durations

    sleep_durations = asyncio.run(scenario())

    # Capped at MAX_SLEEP_SEC either way (a 2030 reset is far off), but the
    # point is it must not be near-zero -- confirms 1893456000 wasn't
    # treated as "1893456000 more seconds from now" nor as "already past".
    from tiktok_monitor.client import MAX_SLEEP_SEC

    assert sleep_durations == [MAX_SLEEP_SEC]


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
@patch.object(SessionRunner, "build_client")
def test_generic_connection_error_still_logged_as_a_connection_error(mock_build_client, mock_check_is_live, caplog):
    async def scenario():
        runner, _conn = make_runner()

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=RuntimeError("some TikTok-side connection problem"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client
        mock_check_is_live.return_value = True  # still live -- not a natural stream end

        with patch("asyncio.sleep", AsyncMock(side_effect=_StopLoop)):
            with caplog.at_level("WARNING", logger="tiktok_monitor.client"):
                try:
                    await run_with_reconnect(runner, runner.settings)
                except _StopLoop:
                    pass

        runner.manual_end()

    asyncio.run(scenario())

    messages = [r.message for r in caplog.records]
    assert any("connection error" in m for m in messages)
    # Must NOT be mistaken for a signature rate limit.
    assert not any("rate limit" in m.lower() for m in messages)


# --- _attach_sign_rate_limit_logger ---------------------------------------


def test_attach_sign_rate_limit_logger_logs_remaining_quota(caplog):
    client = TikTokLiveClient(unique_id="target_streamer")
    _attach_sign_rate_limit_logger(client)

    httpx_client = client.web.signer.sdk_client.get_async_httpx_client()
    hooks = httpx_client.event_hooks["response"]
    assert len(hooks) == 1

    fake_response = MagicMock()
    fake_response.headers = {"RateLimit-Remaining": "42"}

    with caplog.at_level("INFO", logger="tiktok_monitor.client"):
        asyncio.run(hooks[0](fake_response))

    assert any("42" in r.message and "quota" in r.message.lower() for r in caplog.records)


def test_attach_sign_rate_limit_logger_skips_silently_when_header_absent(caplog):
    client = TikTokLiveClient(unique_id="target_streamer")
    _attach_sign_rate_limit_logger(client)
    httpx_client = client.web.signer.sdk_client.get_async_httpx_client()
    hook = httpx_client.event_hooks["response"][0]

    fake_response = MagicMock()
    fake_response.headers = {}  # no RateLimit-Remaining on this response

    with caplog.at_level("INFO", logger="tiktok_monitor.client"):
        asyncio.run(hook(fake_response))

    assert caplog.records == []


def test_attach_sign_rate_limit_logger_does_not_raise_if_library_structure_changes():
    client = MagicMock()
    del client.web  # simulate a future TikTokLive release removing/renaming this accessor

    _attach_sign_rate_limit_logger(client)  # must not raise
