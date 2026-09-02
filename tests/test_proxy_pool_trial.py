import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db, proxy_pool_trial as ppt
from tiktok_monitor.watch import CheckPacer


def make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def make_trial(proxy_urls, pool, events_path):
    conn = make_conn()
    pacer = CheckPacer(pace_sec=0.0)
    return ppt.ProxyPoolTrial(
        conn, proxy_urls, pool, {"db_path": ":memory:", "idle_timeout": None}, pacer, str(events_path)
    )


# --- file parsing --------------------------------------------------------


def test_read_proxy_urls_file_parses_one_per_line_and_skips_blanks_and_comments(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text(
        "http://user:pass@1.2.3.4:8080\n"
        "\n"
        "# a comment\n"
        "http://user:pass@5.6.7.8:8080\n",
        encoding="utf-8",
    )

    result = ppt._read_proxy_urls_file(str(path))

    assert result == ["http://user:pass@1.2.3.4:8080", "http://user:pass@5.6.7.8:8080"]


def test_read_proxy_urls_file_dedups_preserving_order(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text("a\nb\na\n", encoding="utf-8")

    assert ppt._read_proxy_urls_file(str(path)) == ["a", "b"]


def test_masked_hides_credentials():
    assert ppt._masked("http://secretuser:secretpass@1.2.3.4:8080") == "1.2.3.4:8080"


def test_masked_returns_invalid_for_unparseable_url():
    assert ppt._masked("not-a-url") == "invalid"


# --- round-robin slot/username selection ----------------------------------


def test_next_available_slot_round_robins_and_skips_in_use():
    trial = make_trial(["p1", "p2", "p3"], ["u1"], "/tmp/unused.jsonl")
    trial.slots[1].in_use = True  # ip#2 busy

    picked = [trial._next_available_slot().index for _ in range(3)]

    assert picked == [1, 3, 1]  # cycles past the busy one


def test_next_available_slot_returns_none_when_all_in_use():
    trial = make_trial(["p1"], ["u1"], "/tmp/unused.jsonl")
    trial.slots[0].in_use = True

    assert trial._next_available_slot() is None


def test_next_candidate_username_skips_usernames_already_recording():
    trial = make_trial(["p1", "p2"], ["a", "b", "c"], "/tmp/unused.jsonl")
    trial.slots[0].in_use = True
    trial.slots[0].username = "b"

    picked = [trial._next_candidate_username() for _ in range(3)]

    assert "b" not in picked
    assert set(picked) == {"a", "c"}


def test_next_candidate_username_returns_none_when_everyone_already_recording():
    trial = make_trial(["p1", "p2"], ["a", "b"], "/tmp/unused.jsonl")
    trial.slots[0].in_use = True
    trial.slots[0].username = "a"
    trial.slots[1].in_use = True
    trial.slots[1].username = "b"

    assert trial._next_candidate_username() is None


def test_next_available_slot_stays_safe_after_a_slot_is_removed_mid_run(tmp_path):
    # run_forever removes a slot with an invalid proxy URL from self.slots
    # directly -- confirm the round-robin cursor doesn't break (index error
    # or infinite loop) once the list is shorter than when cursor advanced.
    trial = make_trial(["p1", "p2", "p3"], ["u1"], tmp_path / "events.jsonl")
    trial._next_available_slot()  # advance cursor once
    trial.slots.pop(1)  # remove ip#2

    # must not raise, and must still find an available slot
    for _ in range(5):
        slot = trial._next_available_slot()
        assert slot is not None


# --- start/end recording bookkeeping --------------------------------------


def test_start_recording_marks_slot_in_use_and_tracks_max_concurrent(tmp_path):
    events_path = tmp_path / "events.jsonl"
    trial = make_trial(["p1", "p2"], ["a", "b"], events_path)

    async def scenario():
        trial._start_recording(trial.slots[0], "a")
        assert trial.slots[0].in_use is True
        assert trial.slots[0].username == "a"
        assert trial.max_concurrent_seen == 1
        trial.slots[0].task.cancel()  # don't let the real recording task actually run in this test

        trial._start_recording(trial.slots[1], "b")
        assert trial.max_concurrent_seen == 2
        trial.slots[1].task.cancel()
        # let the cancellations actually propagate before the test process exits
        await asyncio.sleep(0)

    asyncio.run(scenario())

    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    assert entries[0]["event"] == "recording_started"
    assert entries[0]["ip_index"] == 1
    assert entries[0]["username"] == "a"
    assert entries[0]["active_count"] == 1
    assert entries[1]["active_count"] == 2


def test_run_forever_records_whoever_is_found_live_and_logs_the_check(tmp_path):
    events_path = tmp_path / "events.jsonl"
    trial = make_trial(
        ["http://u:p@1.1.1.1:8080", "http://u:p@2.2.2.2:8080"], ["streamerA", "streamerB"], events_path
    )

    async def fake_check(username, web_proxy=None, on_error=None):
        await asyncio.sleep(0)  # real suspension point, else run_forever's tight loop never yields
        return username == "streamerA"

    class _StopTest(Exception):
        pass

    async def fake_run_with_reconnect(runner, settings, on_status=None):
        raise _StopTest()  # don't actually try to connect anywhere in a test

    async def scenario():
        with patch.object(trial.pacer, "check", fake_check), \
             patch("tiktok_monitor.proxy_pool_trial.run_with_reconnect", fake_run_with_reconnect):
            task = asyncio.create_task(trial.run_forever())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    check_events = [e for e in entries if e["event"] == "check"]
    assert any(e["username"] == "streamerA" and e["is_live"] is True for e in check_events)
    started = [e for e in entries if e["event"] == "recording_started"]
    assert any(e["username"] == "streamerA" for e in started)
    # streamerB never found live in this test, so it should never have been recorded
    assert all(e["username"] != "streamerB" for e in started)


def test_run_forever_logs_check_error_event_when_the_check_raises(tmp_path):
    events_path = tmp_path / "events.jsonl"
    trial = make_trial(["http://u:p@1.1.1.1:8080"], ["streamerA"], events_path)

    async def fake_check(username, web_proxy=None, on_error=None):
        await asyncio.sleep(0)  # real suspension point, else run_forever's tight loop never yields
        if on_error is not None:
            on_error(RuntimeError("403 blocked"))
        return False

    async def scenario():
        with patch.object(trial.pacer, "check", fake_check):
            task = asyncio.create_task(trial.run_forever())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    check_errors = [e for e in entries if e["event"] == "check_error"]
    assert check_errors
    assert check_errors[0]["ip_index"] == 1
    assert check_errors[0]["username"] == "streamerA"
    assert "403 blocked" in check_errors[0]["error"]


def test_run_forever_drops_an_invalid_proxy_url_instead_of_crashing(tmp_path):
    events_path = tmp_path / "events.jsonl"
    trial = make_trial(["not-a-valid-url", "http://u:p@2.2.2.2:8080"], ["streamerA"], events_path)

    async def fake_check(username, web_proxy=None, on_error=None):
        await asyncio.sleep(0)  # real suspension point, else run_forever's tight loop never yields
        return False

    async def scenario():
        with patch.object(trial.pacer, "check", fake_check):
            task = asyncio.create_task(trial.run_forever())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    assert len(trial.slots) == 1  # the invalid one was dropped
    assert trial.slots[0].proxy_url == "http://u:p@2.2.2.2:8080"
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    assert any(e["event"] == "invalid_proxy_url" for e in entries)
