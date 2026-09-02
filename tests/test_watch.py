import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db, watch


class _StopTest(Exception):
    """Raised from the mocked asyncio.sleep to break watch_loop's while-True
    after the interesting part of one cycle has been observed."""


def make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def make_pacer():
    # pace_sec=0.0 -- these tests exercise watch_loop's control flow, not
    # CheckPacer's real timing (that's covered separately below), so a
    # multi-username sweep shouldn't actually wait out real 5-second gaps.
    return watch.CheckPacer(pace_sec=0.0)


def test_watch_loop_records_first_live_streamer_and_skips_the_rest():
    calls = {"checked": [], "recorded": [], "slept": []}
    active = {"runner": None}

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        calls["checked"].append(username)
        return username == "streamerA"

    async def fake_run_with_reconnect(runner, settings):
        calls["recorded"].append(settings.username)
        assert active["runner"] is runner  # set before the recording call
        return runner

    async def fake_sleep(seconds):
        calls["slept"].append(seconds)
        raise _StopTest()

    conn = make_conn()
    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch.object(watch, "run_with_reconnect", fake_run_with_reconnect), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.watch_loop(
                    conn, ["streamerA", "streamerB"], {"db_path": ":memory:", "idle_timeout": None}, 60.0, active,
                    make_pacer(),
                )
            )
        except _StopTest:
            pass

    assert calls["checked"] == ["streamerA"]  # streamerB never checked this cycle -- first-found-wins
    assert calls["recorded"] == ["streamerA"]
    assert calls["slept"] == [watch.POST_SESSION_COOLDOWN_SEC]
    assert active["runner"] is None  # cleared once recording "ended"


def test_watch_loop_polls_everyone_and_sleeps_full_interval_when_nobody_live():
    calls = {"checked": [], "recorded": [], "slept": []}
    active = {"runner": None}

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        calls["checked"].append(username)
        return False

    async def fake_run_with_reconnect(runner, settings):
        calls["recorded"].append(settings.username)
        return runner

    async def fake_sleep(seconds):
        calls["slept"].append(seconds)
        raise _StopTest()

    conn = make_conn()
    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch.object(watch, "run_with_reconnect", fake_run_with_reconnect), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.watch_loop(
                    conn, ["streamerA", "streamerB"], {"db_path": ":memory:", "idle_timeout": None}, 45.0, active,
                    make_pacer(),
                )
            )
        except _StopTest:
            pass

    assert calls["checked"] == ["streamerA", "streamerB"]  # both checked, neither live
    assert calls["recorded"] == []
    assert calls["slept"] == [45.0]  # poll_interval, not the post-session cooldown


def test_watch_loop_passes_web_proxy_through_to_every_check_is_live_call():
    # The live-status probe runs every poll cycle for every registered
    # streamer -- Phase 5 measurement requires it to go through the same
    # proxy as the actual recording connection, not leak the real IP.
    proxy = httpx.Proxy(url="http://proxy.example.com:8080")
    seen_proxies = []
    active = {"runner": None}

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        seen_proxies.append(web_proxy)
        return False

    async def fake_sleep(_seconds):
        raise _StopTest()

    conn = make_conn()
    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.watch_loop(
                    conn, ["streamerA", "streamerB"], {"db_path": ":memory:", "idle_timeout": None}, 60.0, active,
                    make_pacer(), proxy,
                )
            )
        except _StopTest:
            pass

    assert seen_proxies == [proxy, proxy]


def test_watch_loop_passes_none_web_proxy_when_unconfigured():
    seen_proxies = []
    active = {"runner": None}

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        seen_proxies.append(web_proxy)
        return False

    async def fake_sleep(_seconds):
        raise _StopTest()

    conn = make_conn()
    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.watch_loop(
                    conn, ["streamerA"], {"db_path": ":memory:", "idle_timeout": None}, 60.0, active, make_pacer()
                )
            )
        except _StopTest:
            pass

    assert seen_proxies == [None]


def test_resolve_usernames_returns_override_unchanged_and_ignores_db():
    # Explicit command-line usernames are a fixed override -- the DB's
    # streamer list must never be consulted in this mode, even if it
    # contains completely different accounts.
    conn = make_conn()
    db.get_or_create_streamer(conn, "dbOnlyStreamer")

    result = watch.resolve_usernames(conn, ["explicitA", "explicitB"])

    assert result == ["explicitA", "explicitB"]


def test_resolve_usernames_reads_non_archived_streamers_from_db_when_no_override():
    conn = make_conn()
    db.get_or_create_streamer(conn, "streamerA")
    archived_id = db.get_or_create_streamer(conn, "streamerB")
    db.archive_streamer(conn, archived_id)

    assert watch.resolve_usernames(conn, None) == ["streamerA"]
    assert watch.resolve_usernames(conn, []) == ["streamerA"]  # argparse's nargs="*" default


def test_watch_loop_reads_streamer_list_from_db_when_no_override_given():
    conn = make_conn()
    db.get_or_create_streamer(conn, "streamerA")
    db.get_or_create_streamer(conn, "streamerB")

    calls = {"checked": []}
    active = {"runner": None}

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        calls["checked"].append(username)
        return False

    async def fake_sleep(_seconds):
        raise _StopTest()

    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.watch_loop(conn, None, {"db_path": ":memory:", "idle_timeout": None}, 60.0, active, make_pacer())
            )
        except _StopTest:
            pass

    assert calls["checked"] == ["streamerA", "streamerB"]  # list_streamers' name-ordered result


def test_watch_loop_picks_up_a_streamer_added_to_the_db_mid_run_without_restart():
    # The whole point of the dashboard integration: an addition made while
    # watch.py is already running must be reflected within one poll cycle,
    # not require restarting the process.
    conn = make_conn()
    db.get_or_create_streamer(conn, "streamerA")

    calls = {"checked": [], "slept": 0}
    active = {"runner": None}

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        calls["checked"].append(username)
        return False

    async def fake_sleep(_seconds):
        calls["slept"] += 1
        if calls["slept"] == 1:
            db.get_or_create_streamer(conn, "streamerB")  # added "mid-run", between poll cycles
            return
        raise _StopTest()

    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.watch_loop(conn, None, {"db_path": ":memory:", "idle_timeout": None}, 60.0, active, make_pacer())
            )
        except _StopTest:
            pass

    # Cycle 1 only sees streamerA; cycle 2 (after the mid-run addition) sees both.
    assert calls["checked"] == ["streamerA", "streamerA", "streamerB"]


def test_watch_loop_stops_polling_a_streamer_archived_mid_run():
    conn = make_conn()
    db.get_or_create_streamer(conn, "streamerA")
    streamer_b_id = db.get_or_create_streamer(conn, "streamerB")

    calls = {"checked": [], "slept": 0}
    active = {"runner": None}

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        calls["checked"].append(username)
        return False

    async def fake_sleep(_seconds):
        calls["slept"] += 1
        if calls["slept"] == 1:
            db.archive_streamer(conn, streamer_b_id)  # archived "mid-run"
            return
        raise _StopTest()

    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.watch_loop(conn, None, {"db_path": ":memory:", "idle_timeout": None}, 60.0, active, make_pacer())
            )
        except _StopTest:
            pass

    # Cycle 1 sees both; cycle 2 (after archiving) no longer polls streamerB
    # -- but note nothing here forcibly interrupts a recording already in
    # progress, since resolve_usernames is only consulted between cycles.
    assert calls["checked"] == ["streamerA", "streamerB", "streamerA"]


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_passes_web_proxy_to_tiktok_web_client(mock_web_client_cls):
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(return_value=True)
    mock_web_client_cls.return_value = mock_web

    proxy = httpx.Proxy(url="http://proxy.example.com:8080")
    result = asyncio.run(watch.check_is_live("streamerA", web_proxy=proxy))

    assert result is True
    mock_web_client_cls.assert_called_once_with(web_proxy=proxy)


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_passes_none_when_unconfigured(mock_web_client_cls):
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(return_value=True)
    mock_web_client_cls.return_value = mock_web

    asyncio.run(watch.check_is_live("streamerA"))

    mock_web_client_cls.assert_called_once_with(web_proxy=None)


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_makes_a_single_lightweight_request(mock_web_client_cls):
    # The whole point of this change: fetch_is_live accepts unique_id
    # directly (routing to TikTok's lighter /api-live/user/room/ JSON
    # endpoint) instead of first resolving a room_id via a full-page HTML
    # fetch+parse -- one HTTP request per poll instead of two.
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(return_value=True)
    mock_web.fetch_room_id_from_html = AsyncMock(side_effect=AssertionError("should not be called"))
    mock_web_client_cls.return_value = mock_web

    result = asyncio.run(watch.check_is_live("streamerA"))

    assert result is True
    mock_web.fetch_is_live.assert_awaited_once_with(unique_id="streamerA")
    mock_web.fetch_room_id_from_html.assert_not_awaited()


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_returns_false_when_fetch_is_live_reports_offline(mock_web_client_cls):
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(return_value=False)
    mock_web_client_cls.return_value = mock_web

    result = asyncio.run(watch.check_is_live("streamerA"))

    assert result is False


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_swallows_exceptions_and_returns_false(mock_web_client_cls):
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(side_effect=RuntimeError("network hiccup"))
    mock_web_client_cls.return_value = mock_web

    result = asyncio.run(watch.check_is_live("streamerA"))  # must not raise

    assert result is False


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_invokes_on_error_with_the_swallowed_exception(mock_web_client_cls):
    mock_web = MagicMock()
    exc = RuntimeError("403 blocked")
    mock_web.fetch_is_live = AsyncMock(side_effect=exc)
    mock_web_client_cls.return_value = mock_web
    seen = []

    result = asyncio.run(watch.check_is_live("streamerA", on_error=seen.append))  # must not raise

    assert result is False
    assert seen == [exc]


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_swallows_an_on_error_callback_failure_too(mock_web_client_cls):
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(side_effect=RuntimeError("network hiccup"))
    mock_web_client_cls.return_value = mock_web

    def bad_on_error(exc):
        raise ValueError("callback itself is broken")

    result = asyncio.run(watch.check_is_live("streamerA", on_error=bad_on_error))  # must not raise

    assert result is False


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_closes_the_web_client_on_success(mock_web_client_cls):
    # 2026-08-29: a large (500+) population swept continuously for weeks
    # makes tens of thousands of calls/day -- relying on GC to eventually
    # release each call's httpx.AsyncClient is the regime where an
    # unclosed client actually matters, unlike at small scale.
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(return_value=True)
    mock_web.close = AsyncMock()
    mock_web_client_cls.return_value = mock_web

    result = asyncio.run(watch.check_is_live("streamerA"))

    assert result is True
    mock_web.close.assert_awaited_once()


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_closes_the_web_client_even_when_fetch_is_live_raises(mock_web_client_cls):
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(side_effect=RuntimeError("network hiccup"))
    mock_web.close = AsyncMock()
    mock_web_client_cls.return_value = mock_web

    result = asyncio.run(watch.check_is_live("streamerA"))  # must not raise

    assert result is False
    mock_web.close.assert_awaited_once()


@patch("tiktok_monitor.watch.TikTokWebClient")
def test_check_is_live_swallows_a_close_failure_without_masking_the_result(mock_web_client_cls):
    # close() itself failing (e.g. connection already broken) must never
    # override the actual check result -- best-effort cleanup only.
    mock_web = MagicMock()
    mock_web.fetch_is_live = AsyncMock(return_value=True)
    mock_web.close = AsyncMock(side_effect=RuntimeError("close failed"))
    mock_web_client_cls.return_value = mock_web

    result = asyncio.run(watch.check_is_live("streamerA"))  # must not raise

    assert result is True


def _fake_cleanup_result(calls):
    def fake(conn):
        calls.append(conn)
        return {"retention_days": 3, "session_ids": [], "sessions": 0, "rows": 0}

    return fake


def test_maybe_cleanup_runs_immediately_when_last_cleanup_at_is_zero():
    # last_cleanup_at=0.0 models watch_loop's initial value; `now` must be a
    # realistic epoch-scale timestamp (like the real time.time() the loop
    # passes in) for "now - 0.0 >= interval" to actually hold -- a small
    # `now` like 1000.0 would NOT trigger, since 1000 < a day in seconds.
    conn = make_conn()
    calls = []
    now = 1_800_000_000.0
    with patch.object(watch.cleanup_module, "cleanup_raw_payloads", _fake_cleanup_result(calls)):
        result = watch._maybe_cleanup_raw_payloads(conn, last_cleanup_at=0.0, now=now)

    assert result == now
    assert len(calls) == 1


def test_maybe_cleanup_skips_when_interval_has_not_elapsed():
    conn = make_conn()
    calls = []
    with patch.object(watch.cleanup_module, "cleanup_raw_payloads", lambda c: calls.append(c)):
        result = watch._maybe_cleanup_raw_payloads(conn, last_cleanup_at=1000.0, now=1000.0 + 3600)  # only 1h later

    assert result == 1000.0  # unchanged -- did not run
    assert calls == []


def test_maybe_cleanup_runs_again_after_interval_elapses():
    conn = make_conn()
    calls = []
    later = 1000.0 + watch.RAW_PAYLOAD_CLEANUP_INTERVAL_SEC
    with patch.object(watch.cleanup_module, "cleanup_raw_payloads", _fake_cleanup_result(calls)):
        result = watch._maybe_cleanup_raw_payloads(conn, last_cleanup_at=1000.0, now=later)

    assert result == later
    assert len(calls) == 1


def test_maybe_cleanup_swallows_exceptions_and_still_advances_timestamp():
    # A cleanup failure must never take down the watcher, and must not spam
    # retries every poll cycle -- it waits the full interval again.
    conn = make_conn()

    def boom(_conn):
        raise RuntimeError("disk full")

    now = 1_800_000_000.0
    with patch.object(watch.cleanup_module, "cleanup_raw_payloads", boom):
        result = watch._maybe_cleanup_raw_payloads(conn, last_cleanup_at=0.0, now=now)

    assert result == now  # advanced despite the failure


def test_maybe_cleanup_actually_deletes_eligible_raw_payloads():
    # End-to-end through the real cleanup_raw_payloads module (not mocked),
    # confirming the watch.py wiring calls it correctly.
    from datetime import datetime, timedelta, timezone

    conn = make_conn()
    streamer_id = db.get_or_create_streamer(conn, "example_user")
    session_id = db.create_live_session(conn, streamer_id)
    old_ended_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    conn.execute(
        "UPDATE live_sessions SET status = 'ended', ended_at = ? WHERE id = ?", (old_ended_at, session_id)
    )
    conn.commit()
    event_id = db.insert_event(
        conn, session_id, "comment", "u1", "Nick", payload={"comment": "hi"}, raw_payload={"raw": "data"}
    )

    watch._maybe_cleanup_raw_payloads(conn, last_cleanup_at=0.0, now=1_800_000_000.0)

    assert db.get_raw_payload(conn, event_id) is None
    row = conn.execute("SELECT payload FROM live_events WHERE id = ?", (event_id,)).fetchone()
    assert "hi" in row[0]  # payload untouched


def test_require_proxy_or_exit_passes_silently_when_proxy_configured():
    watch.require_proxy_or_exit("http://proxy.example.com:8080", allow_no_proxy=False, mode_desc="test")
    watch.require_proxy_or_exit("http://proxy.example.com:8080", allow_no_proxy=True, mode_desc="test")


def test_require_proxy_or_exit_blocks_by_default_when_no_proxy_configured(caplog):
    # The whole point: two real-IP accidents happened during testing despite
    # a human being expected to remember TTS_PROXY_URL -- this must refuse
    # to start, not just log a warning.
    with caplog.at_level("ERROR", logger="tiktok_monitor.watch"):
        with pytest.raises(SystemExit) as exc_info:
            watch.require_proxy_or_exit(None, allow_no_proxy=False, mode_desc="no usernames given -- DB-driven real-ops mode")

    assert exc_info.value.code == 1
    assert any("TTS_PROXY_URL" in r.message for r in caplog.records)


def test_require_proxy_or_exit_blocks_in_override_mode_too():
    # Applies uniformly regardless of DB-driven vs explicit-usernames mode --
    # the real-IP risk comes from the missing proxy itself, not from which
    # mode started the process (the incident that prompted this happened
    # during a test/override run).
    with pytest.raises(SystemExit) as exc_info:
        watch.require_proxy_or_exit(None, allow_no_proxy=False, mode_desc="2 username(s) given on the command line")

    assert exc_info.value.code == 1


def test_require_proxy_or_exit_allows_bypass_and_warns_loudly(caplog):
    with caplog.at_level("WARNING", logger="tiktok_monitor.watch"):
        watch.require_proxy_or_exit(None, allow_no_proxy=True, mode_desc="no usernames given -- DB-driven real-ops mode")  # must not raise

    messages = [r.message for r in caplog.records]
    assert any("REAL IP" in m for m in messages)


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
def test_check_pacer_returns_underlying_result(mock_check_is_live):
    async def scenario():
        pacer = watch.CheckPacer(pace_sec=0.0)
        mock_check_is_live.return_value = True

        result = await pacer.check("streamerA")

        assert result is True
        mock_check_is_live.assert_awaited_once_with("streamerA", web_proxy=None, on_error=None)

    asyncio.run(scenario())


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
def test_check_pacer_passes_web_proxy_through(mock_check_is_live):
    async def scenario():
        proxy = httpx.Proxy(url="http://proxy.example.com:8080")
        pacer = watch.CheckPacer(pace_sec=0.0)
        mock_check_is_live.return_value = False

        await pacer.check("streamerA", web_proxy=proxy)

        mock_check_is_live.assert_awaited_once_with("streamerA", web_proxy=proxy, on_error=None)

    asyncio.run(scenario())


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
def test_check_pacer_never_fires_two_checks_faster_than_pace_sec(mock_check_is_live):
    # Mirrors phase5_measure.py's equivalent test for the same reason: this
    # is what stands between watch.py and the empirically-confirmed 403
    # from bursting check_is_live calls (see this module's docstring).
    async def scenario():
        pacer = watch.CheckPacer(pace_sec=0.1)
        mock_check_is_live.return_value = False

        start = time.monotonic()
        await pacer.check("a")
        await pacer.check("b")
        elapsed = time.monotonic() - start

        assert elapsed >= 0.09  # allow a hair of timer jitter below the 0.1s interval

    asyncio.run(scenario())


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
def test_check_pacer_serializes_concurrent_callers(mock_check_is_live):
    """The whole point of the shared lock: even if watch_loop and
    concurrency_poll_loop happen to call pacer.check() at the exact same
    moment, TikTok never sees two requests in flight at once."""
    in_flight = 0
    max_concurrent = 0

    async def fake_check(username, web_proxy=None, on_error=None):
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return False

    async def scenario():
        pacer = watch.CheckPacer(pace_sec=0.0)
        mock_check_is_live.side_effect = fake_check

        await asyncio.gather(pacer.check("a"), pacer.check("b"), pacer.check("c"))

        assert max_concurrent == 1

    asyncio.run(scenario())


def test_concurrency_poll_loop_logs_one_entry_per_sweep(tmp_path):
    conn = make_conn()
    db.get_or_create_streamer(conn, "streamerA")
    db.get_or_create_streamer(conn, "streamerB")
    concurrency_path = str(tmp_path / "concurrent_live.jsonl")

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        return username == "streamerA"

    async def fake_sleep(_seconds):
        raise _StopTest()

    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.concurrency_poll_loop(conn, None, make_pacer(), 60.0, concurrency_path)
            )
        except _StopTest:
            pass

    lines = Path(concurrency_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["checked_count"] == 2
    assert entry["live_count"] == 1
    assert entry["live_usernames"] == ["streamerA"]
    assert "timestamp" in entry


def test_concurrency_poll_loop_reads_streamer_list_from_db_when_no_override(tmp_path):
    conn = make_conn()
    db.get_or_create_streamer(conn, "streamerA")
    archived_id = db.get_or_create_streamer(conn, "streamerB")
    db.archive_streamer(conn, archived_id)
    concurrency_path = str(tmp_path / "concurrent_live.jsonl")

    checked = []

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        checked.append(username)
        return False

    async def fake_sleep(_seconds):
        raise _StopTest()

    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.concurrency_poll_loop(conn, None, make_pacer(), 60.0, concurrency_path)
            )
        except _StopTest:
            pass

    assert checked == ["streamerA"]  # archived streamerB excluded, same as watch_loop


def test_concurrency_poll_loop_passes_web_proxy_to_check_is_live(tmp_path):
    # Directly answers "does the measurement loop also go through the
    # proxy": it must never leak the real IP even though it's a separate
    # loop from watch_loop's recording-trigger scan.
    conn = make_conn()
    db.get_or_create_streamer(conn, "streamerA")
    concurrency_path = str(tmp_path / "concurrent_live.jsonl")
    proxy = httpx.Proxy(url="http://proxy.example.com:8080")
    seen_proxies = []

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        seen_proxies.append(web_proxy)
        return False

    async def fake_sleep(_seconds):
        raise _StopTest()

    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.concurrency_poll_loop(conn, None, make_pacer(), 60.0, concurrency_path, proxy)
            )
        except _StopTest:
            pass

    assert seen_proxies == [proxy]


def test_concurrency_poll_loop_survives_an_append_jsonl_failure(tmp_path):
    # Never let a logging hiccup take down the watcher -- same reasoning as
    # _maybe_cleanup_raw_payloads.
    conn = make_conn()
    db.get_or_create_streamer(conn, "streamerA")
    concurrency_path = str(tmp_path / "concurrent_live.jsonl")
    calls = {"slept": 0}

    async def fake_check_is_live(username, web_proxy=None, on_error=None):
        return False

    async def fake_sleep(_seconds):
        calls["slept"] += 1
        if calls["slept"] >= 2:
            raise _StopTest()

    with patch.object(watch, "check_is_live", fake_check_is_live), \
         patch.object(watch, "_append_jsonl", side_effect=OSError("disk full")), \
         patch("asyncio.sleep", fake_sleep):
        try:
            asyncio.run(
                watch.concurrency_poll_loop(conn, None, make_pacer(), 60.0, concurrency_path)
            )
        except _StopTest:
            pass

    assert calls["slept"] >= 2  # looped past the failed sweep instead of crashing


def test_read_usernames_file_parses_one_per_line_and_skips_blanks_and_comments(tmp_path):
    path = tmp_path / "usernames.txt"
    path.write_text(
        "riria0069\n"
        "\n"
        "# this is a comment\n"
        "rino_ori25\n"
        "  haru04150728  \n"  # surrounding whitespace stripped
        "\n",
        encoding="utf-8",
    )

    result = watch._read_usernames_file(str(path))

    assert result == ["riria0069", "rino_ori25", "haru04150728"]


def test_read_usernames_file_dedups_preserving_first_occurrence_order(tmp_path):
    path = tmp_path / "usernames.txt"
    path.write_text("a\nb\na\nc\nb\n", encoding="utf-8")

    result = watch._read_usernames_file(str(path))

    assert result == ["a", "b", "c"]


def test_read_usernames_file_handles_crlf_line_endings(tmp_path):
    # sample/streamers.txt is CRLF-terminated -- confirm that \r doesn't
    # end up stuck on the username.
    path = tmp_path / "usernames.txt"
    path.write_bytes(b"riria0069\r\nrino_ori25\r\n")

    result = watch._read_usernames_file(str(path))

    assert result == ["riria0069", "rino_ori25"]
