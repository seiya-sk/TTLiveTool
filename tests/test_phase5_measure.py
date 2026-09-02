import asyncio
import csv
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db, phase5_measure
from tiktok_monitor.config import Settings
from tiktok_monitor.phase5_measure import Orchestrator, StepSchedule, _emit_summary, generate_summary, load_pool


# --- load_pool --------------------------------------------------------


def test_load_pool_parses_usernames_ignoring_comments_and_blanks(tmp_path):
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text(
        "# a comment\n\nuser_a\nuser_b\n\n# another\nuser_a\nuser_c\n", encoding="utf-8"
    )
    assert load_pool(str(pool_file)) == ["user_a", "user_b", "user_c"]  # deduped, order preserved


def test_load_pool_returns_empty_list_for_missing_file(tmp_path):
    assert load_pool(str(tmp_path / "does_not_exist.txt")) == []


# --- StepSchedule -------------------------------------------------------


def test_step_schedule_climbs_the_predefined_ladder():
    sched = StepSchedule()
    assert sched.next_after(3) == 5
    assert sched.next_after(5) == 10
    assert sched.next_after(15) == 20


def test_step_schedule_extends_past_the_predefined_ladder():
    sched = StepSchedule()
    assert sched.next_after(20) == 25
    assert sched.next_after(25) == 30


def test_step_schedule_drops_to_the_previous_step():
    sched = StepSchedule()
    assert sched.prev_before(10) == 5
    assert sched.prev_before(5) == 3


def test_step_schedule_floors_at_the_lowest_step():
    sched = StepSchedule()
    assert sched.prev_before(3) == 3


# --- Orchestrator: slot lifecycle -----------------------------------------


_SCRATCH_DIR = tempfile.mkdtemp(prefix="phase5_measure_test_")


def make_orchestrator(conn=None, phase="A", target_count=3, **kwargs):
    conn = conn or db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(username="__placeholder__", db_path=":memory:", idle_timeout_sec=60)
    # Default paths are real (but disposable) files, not a sentinel string --
    # os.fsync (used by the append helpers) fails outright against devnull,
    # and tests that don't care about file contents still exercise the real
    # write path this way rather than a special-cased no-op.
    orch = Orchestrator(
        conn,
        settings,
        proxy=None,
        pool=kwargs.pop("pool", ["a", "b", "c", "d", "e"]),
        phase=phase,
        target_count=target_count,
        metrics_path=kwargs.pop("metrics_path", f"{_SCRATCH_DIR}/metrics_{id(object())}.csv"),
        anomalies_path=kwargs.pop("anomalies_path", f"{_SCRATCH_DIR}/anomalies_{id(object())}.jsonl"),
        stability_sec=kwargs.pop("stability_sec", 3600.0),
        stall_threshold_sec=kwargs.pop("stall_threshold_sec", 180.0),
        pool_check_interval_sec=kwargs.pop("pool_check_interval_sec", phase5_measure.POOL_CHECK_INTERVAL_SEC),
        connection_start_interval_sec=kwargs.pop(
            "connection_start_interval_sec", phase5_measure.CONNECTION_START_INTERVAL_SEC
        ),
        pool_recheck_cooldown_sec=kwargs.pop("pool_recheck_cooldown_sec", phase5_measure.POOL_RECHECK_COOLDOWN_SEC),
        max_reconnects_per_live=kwargs.pop("max_reconnects_per_live", phase5_measure.MAX_RECONNECTS_PER_LIVE),
        problematic_streamers_path=kwargs.pop(
            "problematic_streamers_path", f"{_SCRATCH_DIR}/problematic_{id(object())}.jsonl"
        ),
        session_volume_path=kwargs.pop("session_volume_path", f"{_SCRATCH_DIR}/session_volume_{id(object())}.jsonl"),
    )
    return orch, conn


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_start_slot_creates_a_task_and_tracks_it(mock_run):
    async def scenario():
        orch, _conn = make_orchestrator()
        orch._start_slot("streamer_a")

        assert "streamer_a" in orch.slots
        assert orch.max_active_ever == 1
        mock_run.assert_called_once()

        orch.slots["streamer_a"].task.cancel()
        try:
            await orch.slots["streamer_a"].task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_start_slot_wires_paced_check_is_live_and_the_failure_cap(mock_run):
    """The signature-exhaustion guard only works if these actually reach
    run_with_reconnect -- see the "kana18724 incident" postmortem in
    phase5_measure.py's module docstring."""

    async def scenario():
        orch, _conn = make_orchestrator()
        orch._start_slot("streamer_a")

        _args, kwargs = mock_run.call_args
        assert kwargs["check_is_live_fn"] == orch._paced_check_is_live
        assert kwargs["max_consecutive_failures"] == phase5_measure.MAX_CONSECUTIVE_FAILURES
        assert kwargs["max_reconnects_per_live"] == phase5_measure.MAX_RECONNECTS_PER_LIVE

        orch.slots["streamer_a"].task.cancel()
        try:
            await orch.slots["streamer_a"].task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_reap_finished_slots_frees_the_slot_and_applies_cooldown(mock_run):
    async def scenario():
        orch, _conn = make_orchestrator()
        orch._start_slot("streamer_a")
        await asyncio.sleep(0)  # let the (mocked, instantly-returning) task complete

        orch._reap_finished_slots()

        assert "streamer_a" not in orch.slots
        assert "streamer_a" in orch.cooldown_until
        expected = time.monotonic() + phase5_measure.COOLDOWN_SEC
        assert orch.cooldown_until["streamer_a"] == pytest.approx(expected, abs=1)

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_reap_finished_slots_applies_the_longer_quarantine_for_a_problem_streamer(mock_run):
    """A streamer run_with_reconnect gave up on after repeated
    confirmed-live connection failures (the kana18724 pattern) must not be
    re-picked after the normal short COOLDOWN_SEC -- that would just repeat
    the incident every couple of minutes."""

    async def scenario():
        orch, _conn = make_orchestrator()
        orch._start_slot("streamer_a")
        orch._problem_streamers.add("streamer_a")  # as on_status("gave_up_repeated_failures", ...) would do
        await asyncio.sleep(0)

        orch._reap_finished_slots()

        assert "streamer_a" not in orch.slots
        expected = time.monotonic() + phase5_measure.PROBLEM_STREAMER_COOLDOWN_SEC
        assert orch.cooldown_until["streamer_a"] == pytest.approx(expected, abs=1)
        assert "streamer_a" not in orch._problem_streamers  # consumed, not left set forever

    asyncio.run(scenario())


def test_on_status_gave_up_repeated_failures_records_anomaly_and_marks_problem_streamer():
    orch, _conn = make_orchestrator()
    on_status = orch._make_on_status("streamer_a")

    on_status("gave_up_repeated_failures", {"consecutive_failures": 5, "last_error": "boom"})

    assert orch.anomaly_counts["gave_up_repeated_failures"] == 1
    assert "streamer_a" in orch._problem_streamers


# --- excluded_reconnect_limit: recorded separately, NOT an anomaly --------
#
# Deliberate design choice (confirmed with the user): this is a per-streamer
# flakiness signal, not evidence that target_count itself is unstable, so
# it must never touch anomaly_counts/_step_down -- see MAX_RECONNECTS_PER_LIVE's
# comment in phase5_measure.py.


def test_on_status_excluded_reconnect_limit_marks_problem_streamer_without_recording_an_anomaly(tmp_path):
    orch, _conn = make_orchestrator(target_count=10, problematic_streamers_path=str(tmp_path / "problematic.jsonl"))
    on_status = orch._make_on_status("streamer_a")
    anomaly_counts_before = dict(orch.anomaly_counts)

    on_status(
        "excluded_reconnect_limit",
        {"reconnect_count": 5, "last_error": "InvalidStatusCode(400)", "is_live_at_failure": True, "room_id": 12345},
    )

    assert "streamer_a" in orch._problem_streamers
    assert orch.anomaly_counts == anomaly_counts_before  # unchanged -- not an anomaly
    assert orch.target_count == 10  # unaffected -- no Phase A step-down


def test_record_problematic_streamer_writes_the_requested_fields(tmp_path):
    path = str(tmp_path / "problematic.jsonl")
    orch, _conn = make_orchestrator(target_count=7, problematic_streamers_path=path)

    orch._record_problematic_streamer(
        "baby_8_xo",
        {"reconnect_count": 5, "last_error": "InvalidStatusCode(400)", "is_live_at_failure": True, "room_id": 12345},
    )

    with open(path, encoding="utf-8") as f:
        entry = json.loads(f.readline())

    assert entry["username"] == "baby_8_xo"
    assert entry["reconnect_count"] == 5
    assert entry["last_error"] == "InvalidStatusCode(400)"
    assert entry["is_live_at_failure"] is True
    assert entry["room_id"] == 12345
    assert entry["target_count"] == 7
    assert "timestamp" in entry


def test_on_status_connected_captures_the_session_id_on_the_slot():
    orch, _conn = make_orchestrator()
    fake_runner = MagicMock()
    fake_runner.live_session_id = 42
    orch.slots["streamer_a"] = MagicMock(runner=fake_runner, last_session_id=None)
    on_status = orch._make_on_status("streamer_a")

    on_status("connected", {})

    assert orch.slots["streamer_a"].last_session_id == 42


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_paced_check_is_live_returns_the_underlying_result(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator()
        mock_check_is_live.return_value = True

        result = await orch._paced_check_is_live("streamer_a")

        assert result is True
        mock_check_is_live.assert_awaited_once_with("streamer_a", web_proxy=orch.proxy)

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_paced_check_is_live_swallows_exceptions_as_not_live(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator()
        mock_check_is_live.side_effect = RuntimeError("boom")

        result = await orch._paced_check_is_live("streamer_a")

        assert result is False

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_paced_check_is_live_never_fires_two_checks_faster_than_the_interval(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator(pool_check_interval_sec=0.1)
        mock_check_is_live.return_value = False

        start = time.monotonic()
        await orch._paced_check_is_live("a")
        await orch._paced_check_is_live("b")
        elapsed = time.monotonic() - start

        assert elapsed >= 0.09  # allow a hair of timer jitter below the 0.1s interval

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_paced_check_is_live_serializes_concurrent_callers(mock_check_is_live):
    """The whole point of the shared lock: even if two callers (e.g. pool
    scan + disconnect resolution) ask at the exact same time, TikTok never
    sees two requests in flight at once."""

    in_flight = 0
    max_concurrent = 0

    async def fake_check(username, web_proxy=None):
        nonlocal in_flight, max_concurrent
        in_flight += 1
        max_concurrent = max(max_concurrent, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return False

    async def scenario():
        orch, _conn = make_orchestrator(pool_check_interval_sec=0.0)
        mock_check_is_live.side_effect = fake_check

        await asyncio.gather(
            orch._paced_check_is_live("a"),
            orch._paced_check_is_live("b"),
            orch._paced_check_is_live("c"),
        )

        assert max_concurrent == 1

    asyncio.run(scenario())


# --- round-robin candidate selection (the whole point: never O(pool size)) --


def test_next_candidate_walks_the_pool_in_order():
    orch, _conn = make_orchestrator(pool=["a", "b", "c"], target_count=5)

    assert orch._next_candidate() == "a"
    assert orch._next_candidate() == "b"
    assert orch._next_candidate() == "c"
    assert orch._next_candidate() == "a"  # wraps around


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_next_candidate_skips_active_pending_and_cooldown_usernames(mock_run):
    async def scenario():
        orch, _conn = make_orchestrator(pool=["a", "b", "c"], target_count=5)
        orch._start_slot("a")
        orch._pending_starts.add("b")

        assert orch._next_candidate() == "c"
        orch.slots["a"].task.cancel()

    asyncio.run(scenario())


def test_next_candidate_skips_usernames_still_in_cooldown():
    orch, _conn = make_orchestrator(pool=["a", "b"], target_count=5)
    orch.cooldown_until["a"] = time.monotonic() + 999

    assert orch._next_candidate() == "b"


def test_next_candidate_skips_usernames_checked_within_the_recheck_cooldown():
    orch, _conn = make_orchestrator(pool=["a", "b"], target_count=5, pool_recheck_cooldown_sec=300.0)
    orch.last_checked_at["a"] = time.monotonic()

    assert orch._next_candidate() == "b"


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_next_candidate_returns_none_when_the_whole_pool_is_unavailable(mock_run):
    async def scenario():
        orch, _conn = make_orchestrator(pool=["a", "b"], target_count=5)
        orch._start_slot("a")
        orch._pending_starts.add("b")

        assert orch._next_candidate() is None
        orch.slots["a"].task.cancel()

    asyncio.run(scenario())


def test_next_candidate_returns_none_for_an_empty_pool():
    orch, _conn = make_orchestrator(pool=[], target_count=5)

    assert orch._next_candidate() is None


# --- _pool_check_tick: one check, at most, per call --------------------


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_pool_check_tick_checks_exactly_one_candidate(mock_check_is_live):
    async def scenario():
        big_pool = [f"user_{i}" for i in range(500)]
        orch, _conn = make_orchestrator(target_count=3, pool=big_pool)
        mock_check_is_live.return_value = False

        await orch._pool_check_tick()

        assert mock_check_is_live.await_count == 1  # never O(pool size), one call per tick regardless

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_pool_check_tick_does_nothing_when_already_at_target(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator(target_count=1)
        orch._start_slot("a")

        await orch._pool_check_tick()

        mock_check_is_live.assert_not_awaited()
        orch.slots["a"].task.cancel()

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_pool_check_tick_enqueues_a_confirmed_live_candidate(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator(pool=["a"], target_count=5)
        mock_check_is_live.return_value = True

        await orch._pool_check_tick()

        assert "a" in orch._pending_starts
        assert orch._live_queue.get_nowait() == "a"

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_pool_check_tick_does_not_enqueue_a_not_live_candidate(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator(pool=["a"], target_count=5)
        mock_check_is_live.return_value = False

        await orch._pool_check_tick()

        assert orch._live_queue.empty()
        assert "a" not in orch._pending_starts

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_pool_check_tick_records_the_check_time_so_the_recheck_cooldown_applies(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator(pool=["a"], target_count=5)
        mock_check_is_live.return_value = False

        await orch._pool_check_tick()

        assert "a" in orch.last_checked_at

    asyncio.run(scenario())


# --- _connection_start_tick: one connection start, at most, per call ----


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_connection_start_tick_starts_a_queued_candidate(mock_run):
    async def scenario():
        orch, _conn = make_orchestrator(target_count=5)
        orch._pending_starts.add("a")
        await orch._live_queue.put("a")

        started = await orch._connection_start_tick()

        assert started is True
        assert "a" in orch.slots
        assert "a" not in orch._pending_starts
        orch.slots["a"].task.cancel()

    asyncio.run(scenario())


def test_connection_start_tick_returns_false_when_the_queue_is_empty():
    async def scenario():
        orch, _conn = make_orchestrator(target_count=5)

        started = await orch._connection_start_tick()

        assert started is False

    asyncio.run(scenario())


def test_connection_start_tick_discards_a_stale_candidate_already_at_target():
    async def scenario():
        orch, _conn = make_orchestrator(target_count=1)
        with patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock):
            orch._start_slot("already_connected")
            orch._pending_starts.add("b")
            await orch._live_queue.put("b")

            started = await orch._connection_start_tick()

            assert started is False
            assert "b" not in orch.slots
            orch.slots["already_connected"].task.cancel()

    asyncio.run(scenario())


def test_connection_start_tick_discards_a_candidate_already_connected_another_way():
    async def scenario():
        orch, _conn = make_orchestrator(target_count=5)
        with patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock):
            orch._start_slot("a")  # e.g. started via some other path before this tick ran
            orch._pending_starts.add("a")
            await orch._live_queue.put("a")

            started = await orch._connection_start_tick()

            assert started is False
            orch.slots["a"].task.cancel()

    asyncio.run(scenario())


# --- background loop wrappers: they run the tick + sleep, forever -------


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_pool_check_loop_ticks_repeatedly_until_cancelled(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator(pool=["a"], target_count=5, pool_check_interval_sec=0.0)
        mock_check_is_live.return_value = False

        task = asyncio.create_task(orch._pool_check_loop())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mock_check_is_live.await_count >= 1

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_connection_start_loop_starts_a_queued_candidate_until_cancelled(mock_run):
    async def scenario():
        orch, _conn = make_orchestrator(target_count=5, connection_start_interval_sec=0.0)
        orch._pending_starts.add("a")
        await orch._live_queue.put("a")

        task = asyncio.create_task(orch._connection_start_loop())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert "a" in orch.slots
        orch.slots["a"].task.cancel()

    asyncio.run(scenario())


def test_start_background_loops_creates_two_tasks():
    async def scenario():
        orch, _conn = make_orchestrator()

        orch.start_background_loops()

        assert len(orch._background_tasks) == 2
        orch.shutdown()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_shutdown_cancels_background_loops():
    async def scenario():
        orch, _conn = make_orchestrator()
        orch.start_background_loops()

        orch.shutdown()
        await asyncio.sleep(0)

        assert all(task.cancelled() or task.done() for task in orch._background_tasks)

    asyncio.run(scenario())


# --- anomaly recording + Phase A step-down --------------------------------


def test_record_anomaly_writes_jsonl_and_increments_count(tmp_path):
    anomalies_path = str(tmp_path / "anomalies.jsonl")
    orch, _conn = make_orchestrator(anomalies_path=anomalies_path)

    orch._record_anomaly("connection_error", "streamer_a", {"error": "boom"})

    assert orch.anomaly_counts["connection_error"] == 1
    with open(anomalies_path, encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["anomaly_type"] == "connection_error"
    assert entry["username"] == "streamer_a"
    assert entry["target_count"] == 3
    assert entry["detail"] == {"error": "boom"}


def test_record_anomaly_steps_down_in_phase_a(tmp_path):
    orch, _conn = make_orchestrator(phase="A", target_count=10, anomalies_path=str(tmp_path / "a.jsonl"))

    orch._record_anomaly("connection_error", "streamer_a", {})

    assert orch.target_count == 5  # stepped down from 10 to the previous rung


def test_record_anomaly_does_not_step_down_in_phase_b(tmp_path):
    orch, _conn = make_orchestrator(phase="B", target_count=10, anomalies_path=str(tmp_path / "a.jsonl"))

    orch._record_anomaly("connection_error", "streamer_a", {})

    assert orch.target_count == 10  # Phase B holds the level fixed regardless of anomalies


def test_record_anomaly_includes_connection_duration_when_slot_is_connected(tmp_path):
    anomalies_path = str(tmp_path / "a.jsonl")
    orch, _conn = make_orchestrator(anomalies_path=anomalies_path)
    fake_slot = MagicMock()
    fake_slot.connected_at = time.monotonic() - 42
    orch.slots["streamer_a"] = fake_slot

    orch._record_anomaly("connection_error", "streamer_a", {})

    with open(anomalies_path, encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["connection_duration_sec"] == pytest.approx(42, abs=1)


# --- Phase A step-up ----------------------------------------------------


def test_maybe_step_up_climbs_after_the_stability_window(tmp_path):
    orch, _conn = make_orchestrator(target_count=3, stability_sec=3600.0)
    orch.stability_start = time.monotonic() - 3601  # already past the stability window

    orch._maybe_step_up()

    assert orch.target_count == 5


def test_maybe_step_up_does_nothing_before_the_stability_window_elapses(tmp_path):
    orch, _conn = make_orchestrator(target_count=3, stability_sec=3600.0)
    orch.stability_start = time.monotonic() - 10  # nowhere near stable yet

    orch._maybe_step_up()

    assert orch.target_count == 3


def test_maybe_step_up_never_fires_in_phase_b(tmp_path):
    orch, _conn = make_orchestrator(phase="B", target_count=10, stability_sec=3600.0)
    orch.stability_start = time.monotonic() - 999999

    orch._maybe_step_up()

    assert orch.target_count == 10


# --- on_status wiring ----------------------------------------------------


def test_on_status_records_connection_error_immediately():
    orch, _conn = make_orchestrator()
    on_status = orch._make_on_status("streamer_a")

    on_status("connection_error", {"error": "boom", "delay": 4.0})

    assert orch.anomaly_counts["connection_error"] == 1


def test_on_status_records_signature_rate_limit_immediately():
    orch, _conn = make_orchestrator()
    on_status = orch._make_on_status("streamer_a")

    on_status("signature_rate_limit", {"retry_after": 5, "reset_time": 123})

    assert orch.anomaly_counts["signature_rate_limit"] == 1


def test_on_status_disconnected_is_deferred_not_recorded_immediately():
    orch, _conn = make_orchestrator()
    on_status = orch._make_on_status("streamer_a")

    on_status("disconnected", {})

    assert orch.anomaly_counts["disconnected"] == 0
    assert "streamer_a" in orch.pending_disconnect_since


def test_on_status_connected_clears_a_pending_disconnect():
    orch, _conn = make_orchestrator()
    orch.pending_disconnect_since["streamer_a"] = time.monotonic()
    on_status = orch._make_on_status("streamer_a")

    on_status("connected", {})

    assert "streamer_a" not in orch.pending_disconnect_since


# --- resolving a pending disconnect ---------------------------------------


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_resolve_pending_disconnect_records_anomaly_when_still_live(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator()
        orch.slots["streamer_a"] = MagicMock(connected_at=None)
        orch.pending_disconnect_since["streamer_a"] = time.monotonic() - 999  # well past the grace period
        mock_check_is_live.return_value = True

        await orch._resolve_pending_disconnects()

        assert orch.anomaly_counts["disconnected"] == 1
        assert "streamer_a" not in orch.pending_disconnect_since

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock)
def test_resolve_pending_disconnect_is_not_an_anomaly_when_stream_actually_ended(mock_check_is_live):
    async def scenario():
        orch, _conn = make_orchestrator()
        orch.slots["streamer_a"] = MagicMock()
        orch.pending_disconnect_since["streamer_a"] = time.monotonic() - 999
        mock_check_is_live.return_value = False  # genuinely offline now -- natural end

        await orch._resolve_pending_disconnects()

        assert orch.anomaly_counts["disconnected"] == 0

    asyncio.run(scenario())


def test_resolve_pending_disconnect_waits_for_the_grace_period():
    async def scenario():
        orch, _conn = make_orchestrator()
        orch.slots["streamer_a"] = MagicMock()
        orch.pending_disconnect_since["streamer_a"] = time.monotonic()  # just happened

        with patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock) as mock_check:
            await orch._resolve_pending_disconnects()
            mock_check.assert_not_awaited()

        assert "streamer_a" in orch.pending_disconnect_since  # still pending

    asyncio.run(scenario())


def test_resolve_pending_disconnect_skips_slots_that_already_ended():
    async def scenario():
        orch, _conn = make_orchestrator()
        # No entry in orch.slots -- the slot was reaped before we got to resolve it.
        orch.pending_disconnect_since["streamer_a"] = time.monotonic() - 999

        with patch("tiktok_monitor.phase5_measure.check_is_live", new_callable=AsyncMock) as mock_check:
            await orch._resolve_pending_disconnects()
            mock_check.assert_not_awaited()

        assert orch.anomaly_counts["disconnected"] == 0

    asyncio.run(scenario())


# --- data stall detection --------------------------------------------------


def test_check_data_stalls_flags_a_stale_viewer_count(tmp_path):
    async def scenario():
        orch, conn = make_orchestrator(anomalies_path=str(tmp_path / "a.jsonl"), stall_threshold_sec=180.0)
        streamer_id = db.get_or_create_streamer(conn, "streamer_a")
        session_id = db.create_live_session(conn, streamer_id)
        from datetime import datetime, timedelta, timezone

        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        db.insert_event(
            conn, session_id, "viewer_count", None, None, payload={"viewer_count": 10}, raw_payload={}, occurred_at=stale_time
        )

        fake_slot = MagicMock(connected_at=None)
        fake_slot.runner.live_session_id = session_id
        fake_slot.stalled = False
        orch.slots["streamer_a"] = fake_slot

        await orch._check_data_stalls()

        assert orch.anomaly_counts["data_stall"] == 1
        assert fake_slot.stalled is True

    asyncio.run(scenario())


def test_check_data_stalls_does_not_reflag_an_already_stalled_slot(tmp_path):
    async def scenario():
        orch, conn = make_orchestrator(anomalies_path=str(tmp_path / "a.jsonl"), stall_threshold_sec=180.0)
        streamer_id = db.get_or_create_streamer(conn, "streamer_a")
        session_id = db.create_live_session(conn, streamer_id)
        from datetime import datetime, timedelta, timezone

        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        db.insert_event(
            conn, session_id, "viewer_count", None, None, payload={}, raw_payload={}, occurred_at=stale_time
        )

        fake_slot = MagicMock()
        fake_slot.runner.live_session_id = session_id
        fake_slot.stalled = True  # already flagged on a previous tick
        orch.slots["streamer_a"] = fake_slot

        await orch._check_data_stalls()

        assert orch.anomaly_counts["data_stall"] == 0  # not re-recorded

    asyncio.run(scenario())


def test_check_data_stalls_recovers_when_a_fresh_viewer_count_arrives(tmp_path):
    async def scenario():
        orch, conn = make_orchestrator(anomalies_path=str(tmp_path / "a.jsonl"), stall_threshold_sec=180.0)
        streamer_id = db.get_or_create_streamer(conn, "streamer_a")
        session_id = db.create_live_session(conn, streamer_id)
        from datetime import datetime, timezone

        fresh_time = datetime.now(timezone.utc).isoformat()
        db.insert_event(
            conn, session_id, "viewer_count", None, None, payload={}, raw_payload={}, occurred_at=fresh_time
        )

        fake_slot = MagicMock()
        fake_slot.runner.live_session_id = session_id
        fake_slot.stalled = True
        orch.slots["streamer_a"] = fake_slot

        await orch._check_data_stalls()

        assert fake_slot.stalled is False

    asyncio.run(scenario())


def test_check_data_stalls_skips_sessions_with_no_viewer_count_yet(tmp_path):
    async def scenario():
        orch, conn = make_orchestrator(anomalies_path=str(tmp_path / "a.jsonl"))
        streamer_id = db.get_or_create_streamer(conn, "streamer_a")
        session_id = db.create_live_session(conn, streamer_id)
        # No events inserted at all yet.

        fake_slot = MagicMock()
        fake_slot.runner.live_session_id = session_id
        fake_slot.stalled = False
        orch.slots["streamer_a"] = fake_slot

        await orch._check_data_stalls()

        assert orch.anomaly_counts["data_stall"] == 0

    asyncio.run(scenario())


# --- metrics CSV -----------------------------------------------------------


def test_write_metrics_row_creates_header_then_appends(tmp_path):
    metrics_path = str(tmp_path / "metrics.csv")
    orch, _conn = make_orchestrator(metrics_path=metrics_path)

    orch._write_metrics_row()
    orch._write_metrics_row()

    with open(metrics_path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == phase5_measure.METRICS_HEADER
    assert len(rows) == 3  # header + 2 data rows
    assert rows[1][1] == "A"  # phase
    assert rows[1][2] == "3"  # target_count


def test_write_metrics_row_includes_sign_quota_when_present(tmp_path):
    metrics_path = str(tmp_path / "metrics.csv")
    orch, _conn = make_orchestrator(metrics_path=metrics_path)
    orch.sign_quota_state["sign_quota_remaining"] = 4321

    orch._write_metrics_row()

    with open(metrics_path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    quota_index = phase5_measure.METRICS_HEADER.index("sign_quota_remaining")
    assert rows[1][quota_index] == "4321"


# --- session data volume (Phase 5 cost-model measurement) -----------------


def test_record_session_data_volume_writes_the_measured_fields(tmp_path):
    path = str(tmp_path / "session_volume.jsonl")
    orch, conn = make_orchestrator(session_volume_path=path)
    streamer_id = db.get_or_create_streamer(conn, "streamer_a")
    session_id = db.create_live_session(conn, streamer_id)
    db.insert_event(
        conn, session_id, "comment", user_id=None, user_nickname=None,
        payload={"comment": "hi"}, raw_payload={"raw": "data"},
    )
    db.end_session(conn, session_id, "manual")

    orch._record_session_data_volume("streamer_a", session_id)

    with open(path, encoding="utf-8") as f:
        entry = json.loads(f.readline())

    assert entry["username"] == "streamer_a"
    assert entry["live_session_id"] == session_id
    assert entry["event_count"] == 1
    assert entry["total_bytes"] > 0
    assert "timestamp" in entry


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_reap_finished_slots_records_session_data_volume_when_a_session_existed(mock_run):
    async def scenario():
        orch, _conn = make_orchestrator()
        orch._start_slot("streamer_a")
        orch.slots["streamer_a"].last_session_id = 99
        await asyncio.sleep(0)

        with patch.object(orch, "_record_session_data_volume") as mock_record:
            orch._reap_finished_slots()

        mock_record.assert_called_once_with("streamer_a", 99)

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_reap_finished_slots_skips_data_volume_when_no_session_was_ever_created(mock_run):
    """A slot that ends before ever connecting (user_not_found, etc.) has no
    live_session_id at all -- nothing to query or record."""

    async def scenario():
        orch, _conn = make_orchestrator()
        orch._start_slot("streamer_a")
        assert orch.slots["streamer_a"].last_session_id is None
        await asyncio.sleep(0)

        with patch.object(orch, "_record_session_data_volume") as mock_record:
            orch._reap_finished_slots()

        mock_record.assert_not_called()

    asyncio.run(scenario())


@patch("tiktok_monitor.phase5_measure.run_with_reconnect", new_callable=AsyncMock)
def test_shutdown_records_session_data_volume_for_still_active_slots(mock_run):
    async def scenario():
        orch, _conn = make_orchestrator()
        orch._start_slot("streamer_a")
        orch.slots["streamer_a"].last_session_id = 99

        with patch.object(orch, "_record_session_data_volume") as mock_record:
            orch.shutdown()

        mock_record.assert_called_once_with("streamer_a", 99)

    asyncio.run(scenario())


# --- generate_summary ------------------------------------------------------


def test_generate_summary_handles_no_data(tmp_path):
    summary = generate_summary(str(tmp_path / "missing.csv"), str(tmp_path / "missing.jsonl"))
    assert "メトリクスデータがまだありません" in summary


def test_generate_summary_reports_max_active_and_anomaly_counts(tmp_path):
    metrics_path = tmp_path / "metrics.csv"
    anomalies_path = tmp_path / "anomalies.jsonl"

    with open(metrics_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(phase5_measure.METRICS_HEADER)
        writer.writerow(["2026-01-01T00:00:00+00:00", "A", 3, 3, "5.0", "100.0", "9000", 0, 0, 0, 0, 0])
        writer.writerow(["2026-01-01T01:00:00+00:00", "A", 5, 5, "8.0", "150.0", "8500", 1, 0, 1, 0, 0])

    with open(anomalies_path, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:30:00+00:00",
                    "phase": "A",
                    "target_count": 5,
                    "active_count": 5,
                    "anomaly_type": "connection_error",
                    "username": "streamer_a",
                    "connection_duration_sec": 60,
                    "detail": {},
                }
            )
            + "\n"
        )

    summary = generate_summary(str(metrics_path), str(anomalies_path))

    assert "最大同時本数: 5" in summary
    assert "総異常回数: 1" in summary
    assert "connection_error: 1件" in summary


def test_generate_summary_reports_session_data_volume_distribution(tmp_path):
    metrics_path = tmp_path / "metrics.csv"
    anomalies_path = tmp_path / "anomalies.jsonl"
    session_volume_path = tmp_path / "session_volume.jsonl"

    with open(metrics_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(phase5_measure.METRICS_HEADER)
        writer.writerow(["2026-01-01T00:00:00+00:00", "A", 3, 3, "5.0", "100.0", "9000", 0, 0, 0, 0, 0, 0])
    anomalies_path.write_text("", encoding="utf-8")

    with open(session_volume_path, "w", encoding="utf-8") as f:
        for total_bytes in (1_000_000, 3_000_000, 5_000_000):
            f.write(json.dumps({"event_count": 100, "total_bytes": total_bytes}) + "\n")

    summary = generate_summary(str(metrics_path), str(anomalies_path), str(session_volume_path))

    assert "1配信あたりのデータ量" in summary
    assert "セッション数: 3" in summary


def test_generate_summary_omits_session_data_volume_section_when_no_path_given(tmp_path):
    metrics_path = tmp_path / "metrics.csv"
    anomalies_path = tmp_path / "anomalies.jsonl"
    with open(metrics_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(phase5_measure.METRICS_HEADER)
        writer.writerow(["2026-01-01T00:00:00+00:00", "A", 3, 3, "5.0", "100.0", "9000", 0, 0, 0, 0, 0, 0])
    anomalies_path.write_text("", encoding="utf-8")

    summary = generate_summary(str(metrics_path), str(anomalies_path))

    assert "1配信あたりのデータ量" not in summary


# --- _emit_summary -----------------------------------------------------


def test_emit_summary_writes_utf8_file(tmp_path, capsys):
    metrics_path = tmp_path / "metrics.csv"
    anomalies_path = tmp_path / "anomalies.jsonl"
    summary_path = tmp_path / "summary.txt"

    with open(metrics_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(phase5_measure.METRICS_HEADER)
        writer.writerow(["2026-01-01T00:00:00+00:00", "A", 3, 3, "5.0", "100.0", "9000", 0, 0, 0, 0, 0])

    _emit_summary(str(metrics_path), str(anomalies_path), str(summary_path))

    assert summary_path.exists()
    content = summary_path.read_text(encoding="utf-8")
    assert "実測サマリー" in content
    printed = capsys.readouterr().out
    assert "実測サマリー" in printed
