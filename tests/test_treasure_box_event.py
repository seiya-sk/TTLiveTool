import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner
from tiktok_monitor.config import Settings
from tiktok_monitor.events import is_treasure_box_envelope, normalize_treasure_box_envelope

# Treasure Box and TikTok's unrelated Red Envelope feature are both
# delivered through the same EnvelopeEvent wire message; display_text.key
# is the only reliable discriminator between them (see events.py's Treasure
# Box comment). Field names here (event.common, event.envelope_info) match
# this project's actually-pinned TikTokLive 7.0.0 / TikTokLiveProto v3, not
# the older library version that captured sample/debug_event/*.jsonl (which
# used event.base_message instead) -- see the sample-data test below for how
# that difference is bridged.


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


class FakeEnvelopeEvent:
    """Shaped like a real EnvelopeEvent: common.display_text.key is the
    treasure-box/red-envelope discriminator, envelope_info carries the coin/
    timer fields as typed attributes (not a repr string -- see events.py)."""

    def __init__(
        self,
        key="",
        diamond_count=None,
        people_count=None,
        unpack_at=None,
        send_user_id=None,
        send_user_name=None,
        create_time=1782888088486,
        envelope_id="env-1",
    ):
        self.common = SimpleNamespace(
            display_text=SimpleNamespace(key=key),
            create_time=create_time,
        )
        self.envelope_info = SimpleNamespace(
            envelope_id=envelope_id,
            diamond_count=diamond_count,
            people_count=people_count,
            unpack_at=unpack_at,
            send_user_id=send_user_id,
            send_user_name=send_user_name,
        )


# --- Unit tests -----------------------------------------------------------


def test_is_treasure_box_envelope_true_for_treasure_box_key():
    event = FakeEnvelopeEvent(key="pm_mt_treasure_box_sender_comment", diamond_count=20)
    assert is_treasure_box_envelope(event) is True


def test_is_treasure_box_envelope_false_for_red_envelope():
    event = FakeEnvelopeEvent(key="")
    assert is_treasure_box_envelope(event) is False


def test_normalize_treasure_box_envelope_extracts_fields():
    event = FakeEnvelopeEvent(
        key="pm_mt_treasure_box_sender_comment",
        diamond_count=20,
        people_count=16,
        unpack_at=1782888268,
        send_user_id="7200970294210282498",
        send_user_name="mocomoco217",
        create_time=1782888088486,
        envelope_id="env-42",
    )
    event_type, user_id, nickname, payload = normalize_treasure_box_envelope(event)
    assert event_type == "treasure_box"
    assert user_id == "7200970294210282498"
    assert nickname == "mocomoco217"
    assert payload == {
        "box_id": "env-42",
        "coins": 20,
        "winner_headcount": 16,
        "open_at": 1782888268,
        "sent_at_ms": 1782888088486,
    }


# --- Real sample data verification ----------------------------------------
#
# sample/debug_event/EnvelopeEvent.jsonl was captured by a separate script
# and contains 10 real EnvelopeEvent occurrences: 4 Treasure Box sends and 6
# Red Envelope events, mixed together. Its base_message/envelope_info fields
# are stored as betterproto2 repr() strings (an artifact of that older
# capture script, and of the older TikTokLive version it used -- that
# version named the field base_message instead of common), so this test
# parses just enough out of those strings to rebuild the same typed
# attribute access is_treasure_box_envelope/normalize_treasure_box_envelope
# expect, exercising the actual classifier/normalizer against the real
# captured values, not hand-copied stand-ins.

_KEY_RE = re.compile(r"key='([^']*)'")
_CREATE_TIME_RE = re.compile(r"create_time=(\d+)")
_ENVELOPE_ID_RE = re.compile(r"envelope_id='([^']*)'")
_DIAMOND_RE = re.compile(r"diamond_count=(\d+)")
_PEOPLE_RE = re.compile(r"people_count=(\d+)")
_UNPACK_RE = re.compile(r"unpack_at=(\d+)")
_SEND_NAME_RE = re.compile(r"send_user_name='([^']*)'")
_SEND_ID_RE = re.compile(r"send_user_id='([^']*)'")


def _envelope_event_from_sample_record(data: dict) -> FakeEnvelopeEvent:
    base_message_repr = data.get("base_message") or ""
    envelope_info_repr = data.get("envelope_info") or ""
    key_match = _KEY_RE.search(base_message_repr)
    create_time_match = _CREATE_TIME_RE.search(base_message_repr)
    return FakeEnvelopeEvent(
        key=key_match.group(1) if key_match else "",
        diamond_count=int(m.group(1)) if (m := _DIAMOND_RE.search(envelope_info_repr)) else None,
        people_count=int(m.group(1)) if (m := _PEOPLE_RE.search(envelope_info_repr)) else None,
        unpack_at=int(m.group(1)) if (m := _UNPACK_RE.search(envelope_info_repr)) else None,
        send_user_id=(m.group(1) if (m := _SEND_ID_RE.search(envelope_info_repr)) else None),
        send_user_name=(m.group(1) if (m := _SEND_NAME_RE.search(envelope_info_repr)) else None),
        create_time=int(create_time_match.group(1)) if create_time_match else None,
        envelope_id=(m.group(1) if (m := _ENVELOPE_ID_RE.search(envelope_info_repr)) else None),
    )


_ENVELOPE_SAMPLE = (
    Path(__file__).resolve().parents[1] / "sample" / "debug_event" / "EnvelopeEvent.jsonl"
)

# このファイルは別の採取スクリプトで一度だけ record したもので、再生成できない
# (採取当時の TikTokLive はフィールド名が base_message で、いまの common とは違う)。
# 開発機にはあるが VPS には転送されていない。**捏造して埋めるのは論外** なので
# 存在しない環境では skip する -- ただし理由に復旧手順まで書いておく。
# 恒久 fail を「既知だから」と読み飛ばす習慣がつくほうが、skip より危険。
# 他のテストが FakeEnvelopeEvent で分類ロジック自体は網羅しており、この
# テスト固有の価値は「実採取値に対して」動くことの確認にある。
_SAMPLE_MISSING_REASON = (
    f"実採取サンプルが無い: {_ENVELOPE_SAMPLE} "
    "(開発機の sample/debug_event/EnvelopeEvent.jsonl を転送すれば実行される。"
    "再生成不可のため捏造はしない)"
)


@pytest.mark.skipif(not _ENVELOPE_SAMPLE.exists(), reason=_SAMPLE_MISSING_REASON)
def test_sample_debug_event_envelope_file_splits_4_treasure_box_6_red_envelope():
    lines = [line for line in _ENVELOPE_SAMPLE.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == 10

    treasure_box_count = 0
    red_envelope_count = 0
    for line in lines:
        record = json.loads(line)
        event = _envelope_event_from_sample_record(record["data"])
        if is_treasure_box_envelope(event):
            treasure_box_count += 1
            event_type, _user_id, _nickname, payload = normalize_treasure_box_envelope(event)
            assert event_type == "treasure_box"
            assert payload["coins"] == 20
            assert payload["winner_headcount"] == 16
            assert payload["open_at"] is not None
            assert payload["sent_at_ms"] is not None
        else:
            red_envelope_count += 1
            assert not is_treasure_box_envelope(event)

    assert treasure_box_count == 4
    assert red_envelope_count == 6


# --- Integration: SessionRunner._handle_envelope_event --------------------


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_handle_envelope_event_records_treasure_box(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        event = FakeEnvelopeEvent(
            key="pm_mt_treasure_box_sender_comment",
            diamond_count=20,
            people_count=16,
            unpack_at=1782888268,
            send_user_id="uid-1",
            send_user_name="mocomoco217",
            create_time=1782888088486,
        )

        runner._handle_envelope_event(event)

        row = conn.execute(
            "SELECT event_type, user_id, user_nickname, payload, occurred_at FROM live_events WHERE event_type='treasure_box'"
        ).fetchone()
        assert row is not None
        assert row[0] == "treasure_box"
        assert row[1] == "uid-1"
        assert row[2] == "mocomoco217"
        assert '"coins": 20' in row[3] or '"coins":20' in row[3]
        assert row[4].startswith("2026-07-01")  # 1782888088486ms -> 2026-07-01T06:41:28+00:00

        await _finish(runner)

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_handle_envelope_event_does_not_record_red_envelope(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        event = FakeEnvelopeEvent(key="")  # red envelope: no treasure-box key

        runner._handle_envelope_event(event)

        count = conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0]
        assert count == 0

        await _finish(runner)

    asyncio.run(scenario())


class _ExplodingDisplayText:
    """Simulates an unexpected future field-shape change (e.g. .key becoming
    a raising property) -- getattr(obj, name, default) only swallows
    AttributeError, so anything else still propagates and must be caught by
    the handler's own try/except."""

    @property
    def key(self):
        raise RuntimeError("schema changed")


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_handle_envelope_event_survives_unexpected_parse_error(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        event = FakeEnvelopeEvent(key="pm_mt_treasure_box_sender_comment")
        event.common.display_text = _ExplodingDisplayText()

        runner._handle_envelope_event(event)  # must not raise

        count = conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0]
        assert count == 0  # skipped, not crashed

        await _finish(runner)

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_handle_envelope_event_starts_a_session_if_none_active(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        assert runner.live_session_id is None

        runner._handle_envelope_event(
            FakeEnvelopeEvent(key="pm_mt_treasure_box_sender_comment", diamond_count=20)
        )

        assert runner.live_session_id is not None
        await _finish(runner)

    asyncio.run(scenario())


@patch("tiktok_monitor.client.screenshot_module.capture_live_screenshot", new_callable=AsyncMock, return_value=False)
def test_treasure_box_raw_payload_is_preserved(_mock_capture):
    async def scenario():
        runner, conn = make_runner()
        event = FakeEnvelopeEvent(key="pm_mt_treasure_box_sender_comment", diamond_count=20)

        runner._handle_envelope_event(event)

        event_id = conn.execute("SELECT id FROM live_events WHERE event_type='treasure_box'").fetchone()[0]
        raw = db.get_raw_payload(conn, event_id)
        assert raw is not None

        await _finish(runner)

    asyncio.run(scenario())
