import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from TikTokLive.client.errors import SignatureRateLimitError

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner, run_with_reconnect
from tiktok_monitor.config import Settings

# Phase 5 concurrent-measurement prep: run_with_reconnect/build_client's
# optional on_status callback is the only way an orchestrator running many
# SessionRunners at once can tell which streamer a given status change
# belongs to (several of the existing log messages don't include the
# username -- see client.py's StatusCallback comment). These tests exercise
# that callback directly, without a real TikTok connection.


def make_runner():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(username="target_streamer", db_path=":memory:", idle_timeout_sec=60)
    runner = SessionRunner(conn, settings)
    return runner, conn


# client.current_room_id() は str に正規化するので、期待値も str。
# DB の room_id 列は TEXT で、int と str が混ざると比較が成立しない。
FAKE_ROOM_ID = "1234567890123456789"


class FakeTikTokLiveClient:
    """Enough of TikTokLiveClient's surface for build_client() to wire up
    listeners against, with .on(...)-registered handlers captured so tests
    can invoke them directly to simulate TikTokLive firing that event."""

    def __init__(self):
        self.web = MagicMock()
        self.connected = False
        self.handlers: dict[type, list] = {}
        self.parse_error_ignorelist: list[str] = []
        self.room_id = int(FAKE_ROOM_ID)

    def on(self, event_cls):
        def decorator(fn):
            self.handlers.setdefault(event_cls, []).append(fn)
            return fn

        return decorator

    def add_listener(self, event_cls, fn):
        self.handlers.setdefault(event_cls, []).append(fn)

    async def _parse_webcast_response_message(self, webcast_response=None, webcast_response_message=None):
        return [MagicMock()]  # stand-in "response_event"; never exercised by these tests

    async def fire(self, event_cls, event=None):
        for fn in self.handlers.get(event_cls, []):
            await fn(event)


def make_signature_rate_limit_error(remaining="5", reset="1893456000"):
    fake_response = MagicMock()
    fake_response.headers = {"RateLimit-Remaining": remaining, "RateLimit-Reset": reset}
    return SignatureRateLimitError(
        None, "Too many connections started, try again in %s seconds.", response=fake_response
    )


class _StopLoop(Exception):
    """Raised from the mocked asyncio.sleep to break run_with_reconnect's
    while-loop after the interesting part of one iteration has run."""


# --- build_client: connected/disconnected -------------------------------


def test_build_client_notifies_connected_and_disconnected():
    from TikTokLive.events import ConnectEvent, DisconnectEvent

    async def scenario():
        runner, _conn = make_runner()
        calls = []

        with patch("tiktok_monitor.client.TikTokLiveClient", return_value=FakeTikTokLiveClient()):
            client = runner.build_client(on_status=lambda kind, info: calls.append((kind, info)))

        await client.fire(ConnectEvent)
        await client.fire(DisconnectEvent)

        # connected は room_id も通知する(2026-09-01: セッション分割の調査で、
        # どの room に繋がったかが events.jsonl から追えるようにしたため)
        assert calls[0] == ("connected", {"username": "target_streamer", "room_id": FAKE_ROOM_ID})
        assert calls[1] == ("disconnected", {"username": "target_streamer"})

        runner.manual_end()

    asyncio.run(scenario())


def test_build_client_works_with_no_on_status():
    from TikTokLive.events import ConnectEvent

    async def scenario():
        runner, _conn = make_runner()
        with patch("tiktok_monitor.client.TikTokLiveClient", return_value=FakeTikTokLiveClient()):
            client = runner.build_client()  # no on_status -- must not raise

        await client.fire(ConnectEvent)  # must not raise
        runner.manual_end()

    asyncio.run(scenario())


def test_build_client_silences_known_harmless_parse_errors():
    """WebcastLinkLayerMessage's HashtagNamespace parse failure floods logs
    with a full traceback per occurrence despite being unrelated to any
    event this project records -- confirmed via TikTokLive's own
    client.parse_error_ignorelist (see build_client)."""
    from tiktok_monitor.client import _KNOWN_HARMLESS_PARSE_ERRORS

    runner, _conn = make_runner()
    client = runner.build_client()  # real TikTokLiveClient -- no network I/O happens at construction

    for fingerprint in _KNOWN_HARMLESS_PARSE_ERRORS:
        assert fingerprint in client.parse_error_ignorelist

    runner.manual_end()


# --- run_with_reconnect branches ------------------------------------------


@patch.object(SessionRunner, "build_client")
def test_signature_rate_limit_notifies_with_retry_after_and_reset_time(mock_build_client):
    async def scenario():
        runner, _conn = make_runner()
        calls = []

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=make_signature_rate_limit_error("5", "1893456000"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client

        with patch("asyncio.sleep", AsyncMock(side_effect=_StopLoop)):
            try:
                await run_with_reconnect(
                    runner, runner.settings, on_status=lambda kind, info: calls.append((kind, info))
                )
            except _StopLoop:
                pass

        assert len(calls) == 1
        kind, info = calls[0]
        assert kind == "signature_rate_limit"
        assert info["username"] == "target_streamer"
        assert info["retry_after"] == 5
        assert info["reset_time"] == 1893456000

        runner.manual_end()

    asyncio.run(scenario())


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
@patch.object(SessionRunner, "build_client")
def test_connection_error_notifies_with_error_repr(mock_build_client, mock_check_is_live):
    async def scenario():
        runner, _conn = make_runner()
        calls = []

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=RuntimeError("boom"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client
        mock_check_is_live.return_value = True  # still live -- not a natural stream end

        with patch("asyncio.sleep", AsyncMock(side_effect=_StopLoop)):
            try:
                await run_with_reconnect(
                    runner, runner.settings, on_status=lambda kind, info: calls.append((kind, info))
                )
            except _StopLoop:
                pass

        assert len(calls) == 1
        kind, info = calls[0]
        assert kind == "connection_error"
        assert info["username"] == "target_streamer"
        assert "boom" in info["error"]
        assert info["is_live_at_failure"] is True
        assert info["consecutive_failures"] == 1

        runner.manual_end()

    asyncio.run(scenario())


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
@patch.object(SessionRunner, "build_client")
def test_connection_error_treated_as_natural_end_when_no_longer_live(mock_build_client, mock_check_is_live):
    async def scenario():
        runner, _conn = make_runner()
        calls = []

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=RuntimeError("InvalidStatusCode(400, ...)"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client
        mock_check_is_live.return_value = False  # confirmed offline -- the stream just ended

        await run_with_reconnect(runner, runner.settings, on_status=lambda kind, info: calls.append((kind, info)))

        assert calls == [
            ("user_offline_exit", {"username": "target_streamer", "detail": "offline confirmed after a connection error"})
        ]

    asyncio.run(scenario())


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
@patch.object(SessionRunner, "build_client")
def test_gives_up_after_max_consecutive_failures_while_still_live(mock_build_client, mock_check_is_live):
    async def scenario():
        runner, _conn = make_runner()
        calls = []

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=RuntimeError("InvalidStatusCode(400, ...)"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client
        mock_check_is_live.return_value = True  # still live every time -- never resolves on its own

        sleep_durations = []

        async def fake_sleep(seconds):
            sleep_durations.append(seconds)

        with patch("asyncio.sleep", fake_sleep):
            await run_with_reconnect(
                runner,
                runner.settings,
                on_status=lambda kind, info: calls.append((kind, info)),
                max_consecutive_failures=3,
            )

        kinds = [kind for kind, _info in calls]
        assert kinds.count("connection_error") == 3
        assert kinds[-1] == "gave_up_repeated_failures"
        assert calls[-1][1]["consecutive_failures"] == 3

        # The core bugfix: backoff must actually escalate across repeated
        # post-start (in-task) failures with no genuine connect in between
        # -- previously it stayed pinned at the initial delay forever
        # because client.start() returning without error (even without a
        # real ConnectEvent) wrongly reset it every iteration.
        assert sleep_durations == [2.0, 4.0]  # 2 sleeps between 3 attempts; delay doubled after the first

    asyncio.run(scenario())


@patch("tiktok_monitor.watch.check_is_live", new_callable=AsyncMock)
@patch.object(SessionRunner, "build_client")
def test_unlimited_retries_by_default_even_with_repeated_confirmed_live_failures(mock_build_client, mock_check_is_live):
    async def scenario():
        runner, _conn = make_runner()
        calls = []

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=RuntimeError("InvalidStatusCode(400, ...)"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client
        mock_check_is_live.return_value = True

        attempt_count = {"n": 0}

        async def fake_sleep(_seconds):
            attempt_count["n"] += 1
            if attempt_count["n"] >= 6:  # well past what a finite cap would have allowed
                raise _StopLoop()

        with patch("asyncio.sleep", fake_sleep):
            try:
                await run_with_reconnect(
                    runner, runner.settings, on_status=lambda kind, info: calls.append((kind, info))
                )
            except _StopLoop:
                pass

        assert not any(kind == "gave_up_repeated_failures" for kind, _info in calls)
        assert sum(1 for kind, _info in calls if kind == "connection_error") == 6

        runner.manual_end()

    asyncio.run(scenario())


def test_offline_retry_notifies_after_a_prior_successful_connect():
    from TikTokLive.client.errors import UserOfflineError
    from TikTokLive.events import ConnectEvent

    async def scenario():
        runner, _conn = make_runner()
        calls = []
        created_clients = []

        call_count = {"n": 0}

        async def fake_start(*_a, **_kw):
            call_count["n"] += 1
            fc = created_clients[-1]
            if call_count["n"] == 1:
                # A genuine ConnectEvent -- not just client.start() returning
                # without error -- is what should count as "really
                # connected" (see run_with_reconnect's connected_this_attempt
                # tracking, added after the kana18724 incident where
                # start() succeeding alone was wrongly treated as proof of
                # a working connection).
                await fc.fire(ConnectEvent)

                async def _dummy_task():
                    pass

                return _dummy_task()
            raise UserOfflineError("offline")

        def make_fake_client(*_a, **_kw):
            fc = FakeTikTokLiveClient()
            fc.start = fake_start
            created_clients.append(fc)
            return fc

        # First sleep (after the successful connect) must pass through so a
        # second iteration -- where UserOfflineError fires -- actually
        # happens; only the second sleep call stops the loop.
        sleep_call_count = {"n": 0}

        async def fake_sleep(_seconds):
            sleep_call_count["n"] += 1
            if sleep_call_count["n"] >= 2:
                raise _StopLoop()

        with patch("tiktok_monitor.client.TikTokLiveClient", side_effect=make_fake_client), patch(
            "asyncio.sleep", fake_sleep
        ):
            try:
                await run_with_reconnect(
                    runner, runner.settings, on_status=lambda kind, info: calls.append((kind, info))
                )
            except _StopLoop:
                pass

        # delay resets to the initial value after a genuine connect (fixed
        # behavior -- previously it reset merely because client.start()
        # returned without error, even without a real ConnectEvent, which
        # is exactly the bug that let the kana18724 incident's backoff stay
        # pinned at the initial delay forever instead of escalating).
        expected_delay = runner.settings.reconnect_initial_delay_sec
        assert ("connected", {"username": "target_streamer", "room_id": FAKE_ROOM_ID}) in calls
        assert ("offline_retry", {"username": "target_streamer", "delay": expected_delay}) in calls

        runner.manual_end()

    asyncio.run(scenario())


# --- max_reconnects_per_live: the "flapping" pattern max_consecutive_failures misses ---
#
# 2026-08-27 postmortem, round two: a streamer that connects fine,
# disconnects moments later, reconnects fine, disconnects again -- forever
# -- never trips max_consecutive_failures (each cycle "succeeds" before
# failing again, resetting that counter every time), yet spends one
# signature per reconnect all night. reconnect_count is a separate counter
# that never resets, incremented once per loop iteration regardless of
# cause (a clean disconnect included, which raises no exception at all).


def test_excludes_after_max_reconnects_even_with_no_exceptions_ever():
    from TikTokLive.events import ConnectEvent

    async def scenario():
        runner, _conn = make_runner()
        calls = []
        created_clients = []

        async def fake_start(*_a, **_kw):
            fc = created_clients[-1]
            await fc.fire(ConnectEvent)  # "succeeds" every single time

            async def _dummy_task():
                pass  # then immediately, cleanly disconnects -- no exception at all

            return _dummy_task()

        def make_fake_client(*_a, **_kw):
            fc = FakeTikTokLiveClient()
            fc.start = fake_start
            created_clients.append(fc)
            return fc

        sleep_calls = {"n": 0}

        async def fake_sleep(_seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 20:  # safety net only -- the cap should stop this long before 20
                raise AssertionError("max_reconnects_per_live never tripped")

        with patch("tiktok_monitor.client.TikTokLiveClient", side_effect=make_fake_client), patch(
            "asyncio.sleep", fake_sleep
        ):
            await run_with_reconnect(
                runner,
                runner.settings,
                on_status=lambda kind, info: calls.append((kind, info)),
                max_reconnects_per_live=3,
            )

        kinds = [kind for kind, _info in calls]
        assert kinds.count("connected") == 4  # 1 initial connect + 3 reconnects
        assert kinds[-1] == "excluded_reconnect_limit"
        last_info = calls[-1][1]
        assert last_info["reconnect_count"] == 3
        assert last_info["room_id"] == FAKE_ROOM_ID
        # No connection_error/UserOfflineError ever fired -- this is exactly
        # the pattern max_consecutive_failures can't see.
        assert "connection_error" not in kinds
        assert "gave_up_repeated_failures" not in kinds

        runner.manual_end()

    asyncio.run(scenario())


def test_does_not_exclude_before_reaching_the_reconnect_cap():
    from TikTokLive.events import ConnectEvent

    async def scenario():
        runner, _conn = make_runner()
        calls = []
        created_clients = []

        async def fake_start(*_a, **_kw):
            fc = created_clients[-1]
            await fc.fire(ConnectEvent)

            async def _dummy_task():
                pass

            return _dummy_task()

        def make_fake_client(*_a, **_kw):
            fc = FakeTikTokLiveClient()
            fc.start = fake_start
            created_clients.append(fc)
            return fc

        class _StopAfterOneSleep(Exception):
            pass

        async def fake_sleep(_seconds):
            raise _StopAfterOneSleep()

        with patch("tiktok_monitor.client.TikTokLiveClient", side_effect=make_fake_client), patch(
            "asyncio.sleep", fake_sleep
        ):
            try:
                await run_with_reconnect(
                    runner,
                    runner.settings,
                    on_status=lambda kind, info: calls.append((kind, info)),
                    max_reconnects_per_live=3,
                )
            except _StopAfterOneSleep:
                pass

        # Only the first (non-reconnect) attempt happened -- 0 reconnects so far, well under cap=3.
        assert not any(kind == "excluded_reconnect_limit" for kind, _info in calls)

        runner.manual_end()

    asyncio.run(scenario())


def test_unlimited_reconnects_by_default():
    from TikTokLive.events import ConnectEvent

    async def scenario():
        runner, _conn = make_runner()
        calls = []
        created_clients = []

        async def fake_start(*_a, **_kw):
            fc = created_clients[-1]
            await fc.fire(ConnectEvent)

            async def _dummy_task():
                pass

            return _dummy_task()

        def make_fake_client(*_a, **_kw):
            fc = FakeTikTokLiveClient()
            fc.start = fake_start
            created_clients.append(fc)
            return fc

        sleep_calls = {"n": 0}

        class _StopLoopHere(Exception):
            pass

        async def fake_sleep(_seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 10:
                raise _StopLoopHere()

        with patch("tiktok_monitor.client.TikTokLiveClient", side_effect=make_fake_client), patch(
            "asyncio.sleep", fake_sleep
        ):
            try:
                await run_with_reconnect(
                    runner, runner.settings, on_status=lambda kind, info: calls.append((kind, info))
                )  # max_reconnects_per_live not passed -- default None
            except _StopLoopHere:
                pass

        assert not any(kind == "excluded_reconnect_limit" for kind, _info in calls)

        runner.manual_end()

    asyncio.run(scenario())


# --- 2026-08-28 correction: offline_retry must NOT count toward the cap ---
#
# client.start() checks is_live BEFORE signing, so a plain UserOfflineError
# (offline_retry) never spends a signature -- confirmed in production the
# hard way: a night with zero real flapping still filled
# problematic_streamers.jsonl with streamers that had simply ended their
# broadcast, wrongly quarantining them and shrinking the healthy-streamer
# pool the whole experiment was measuring.


def test_offline_retry_never_counts_toward_the_reconnect_cap():
    from TikTokLive.client.errors import UserOfflineError
    from TikTokLive.events import ConnectEvent

    async def scenario():
        runner, _conn = make_runner()
        calls = []
        created_clients = []

        call_count = {"n": 0}

        async def fake_start(*_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                fc = created_clients[-1]
                await fc.fire(ConnectEvent)  # the one real connect, so ever_connected becomes True

                async def _dummy_task():
                    pass

                return _dummy_task()
            raise UserOfflineError("offline")  # every attempt after that: just offline, forever

        def make_fake_client(*_a, **_kw):
            fc = FakeTikTokLiveClient()
            fc.start = fake_start
            created_clients.append(fc)
            return fc

        sleep_calls = {"n": 0}

        async def fake_sleep(_seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 10:  # far more than max_reconnects_per_live=2 below
                raise _StopLoop()

        with patch("tiktok_monitor.client.TikTokLiveClient", side_effect=make_fake_client), patch(
            "asyncio.sleep", fake_sleep
        ):
            try:
                await run_with_reconnect(
                    runner,
                    runner.settings,
                    on_status=lambda kind, info: calls.append((kind, info)),
                    max_reconnects_per_live=2,
                )
            except _StopLoop:
                pass

        assert not any(kind == "excluded_reconnect_limit" for kind, _info in calls)
        assert sum(1 for kind, _info in calls if kind == "offline_retry") >= 9

        runner.manual_end()

    asyncio.run(scenario())


def test_recovery_reconnect_after_offline_still_counts_toward_the_cap():
    """The tricky boundary case: connects, goes offline, comes back live,
    reconnects, goes offline again... Each offline_retry leg is free and
    skipped, but each RECOVERY reconnect passed is_live and cost a
    signature, so it must still count -- no separate counter needed."""
    from TikTokLive.client.errors import UserOfflineError
    from TikTokLive.events import ConnectEvent

    async def scenario():
        runner, _conn = make_runner()
        calls = []
        created_clients = []

        # Pattern: connect(1), offline, connect(2)=recovery#1, offline, connect(3)=recovery#2 -> excluded
        call_count = {"n": 0}

        async def fake_start(*_a, **_kw):
            call_count["n"] += 1
            n = call_count["n"]
            if n % 2 == 1:  # odd calls: a real (re)connect
                fc = created_clients[-1]
                await fc.fire(ConnectEvent)

                async def _dummy_task():
                    pass

                return _dummy_task()
            raise UserOfflineError("offline")  # even calls: a brief offline blip

        def make_fake_client(*_a, **_kw):
            fc = FakeTikTokLiveClient()
            fc.start = fake_start
            created_clients.append(fc)
            return fc

        async def fake_sleep(_seconds):
            pass  # let it run to completion -- the exclusion should stop it well before any safety net is needed

        with patch("tiktok_monitor.client.TikTokLiveClient", side_effect=make_fake_client), patch(
            "asyncio.sleep", fake_sleep
        ):
            await run_with_reconnect(
                runner,
                runner.settings,
                on_status=lambda kind, info: calls.append((kind, info)),
                max_reconnects_per_live=2,
            )

        kinds = [kind for kind, _info in calls]
        assert kinds.count("connected") == 3  # initial connect + 2 recovery reconnects
        assert kinds.count("offline_retry") == 2
        assert kinds[-1] == "excluded_reconnect_limit"
        assert calls[-1][1]["reconnect_count"] == 2  # the 2 recovery reconnects, NOT the 2 offline blips

        runner.manual_end()

    asyncio.run(scenario())


@patch.object(SessionRunner, "build_client")
def test_signature_rate_limit_does_not_count_toward_the_reconnect_cap(mock_build_client):
    """An account-wide quota condition isn't evidence THIS streamer is
    flaky -- excluded from the cap for the same reason it's excluded from
    consecutive_failures."""

    async def scenario():
        runner, _conn = make_runner()
        calls = []

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=make_signature_rate_limit_error("0", "60"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client

        sleep_calls = {"n": 0}

        async def fake_sleep(_seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 5:  # more than max_reconnects_per_live=2 below
                raise _StopLoop()

        with patch("asyncio.sleep", fake_sleep):
            try:
                await run_with_reconnect(
                    runner,
                    runner.settings,
                    on_status=lambda kind, info: calls.append((kind, info)),
                    max_reconnects_per_live=2,
                )
            except _StopLoop:
                pass

        assert not any(kind == "excluded_reconnect_limit" for kind, _info in calls)
        assert sum(1 for kind, _info in calls if kind == "signature_rate_limit") >= 5

        runner.manual_end()

    asyncio.run(scenario())


@patch.object(SessionRunner, "build_client")
def test_user_not_found_notifies_and_ends(mock_build_client):
    from TikTokLive.client.errors import UserNotFoundError

    async def scenario():
        runner, _conn = make_runner()
        calls = []

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=UserNotFoundError("target_streamer", "nope"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client

        await run_with_reconnect(runner, runner.settings, on_status=lambda kind, info: calls.append((kind, info)))

        assert calls == [("user_not_found", {"username": "target_streamer"})]

    asyncio.run(scenario())


@patch.object(SessionRunner, "build_client")
def test_on_status_exception_does_not_break_the_reconnect_loop(mock_build_client):
    async def scenario():
        runner, conn = make_runner()

        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=RuntimeError("boom"))
        fake_client.connected = False
        mock_build_client.return_value = fake_client

        def exploding_callback(_kind, _info):
            raise RuntimeError("callback bug")

        with patch("asyncio.sleep", AsyncMock(side_effect=_StopLoop)):
            try:
                await run_with_reconnect(runner, runner.settings, on_status=exploding_callback)  # must not raise
            except _StopLoop:
                pass

        runner.manual_end()

    asyncio.run(scenario())


# --- sign quota shared state -----------------------------------------------


def test_attach_sign_rate_limit_logger_writes_to_shared_quota_state():
    from TikTokLive import TikTokLiveClient
    from tiktok_monitor.client import _attach_sign_rate_limit_logger

    client = TikTokLiveClient(unique_id="target_streamer")
    quota_state = {}
    _attach_sign_rate_limit_logger(client, quota_state=quota_state)

    hook = client.web.signer.sdk_client.get_async_httpx_client().event_hooks["response"][0]
    fake_response = MagicMock()
    fake_response.headers = {"RateLimit-Remaining": "77"}

    asyncio.run(hook(fake_response))

    assert quota_state["sign_quota_remaining"] == 77


def test_attach_sign_rate_limit_logger_ignores_missing_state_dict():
    from TikTokLive import TikTokLiveClient
    from tiktok_monitor.client import _attach_sign_rate_limit_logger

    client = TikTokLiveClient(unique_id="target_streamer")
    _attach_sign_rate_limit_logger(client)  # quota_state=None -- must not raise

    hook = client.web.signer.sdk_client.get_async_httpx_client().event_hooks["response"][0]
    fake_response = MagicMock()
    fake_response.headers = {"RateLimit-Remaining": "77"}

    asyncio.run(hook(fake_response))  # must not raise
