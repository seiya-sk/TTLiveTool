import asyncio
import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.client import _attach_gift_parse_failure_capture, _record_gift_parse_failure

# 2026-08-27 gift-loss investigation: WebcastGiftMessage payloads
# intermittently fail to parse (a TikTokLiveProto forward-reference bug in
# SchemeInfo.scheme_gift_type, not wire-format corruption -- confirmed
# harmless for other message types like WebcastLinkLayerMessage/
# HashtagNamespace, but gifts are core recorded data). These tests cover
# the mitigation: capture the raw payload before the noisy traceback gets
# demoted, and never touch the normal (successful) parse path.


def make_response_message(method: str, payload: bytes = b"raw-protobuf-bytes"):
    msg = MagicMock()
    msg.method = method
    msg.payload = payload
    return msg


# --- _record_gift_parse_failure -------------------------------------------


def test_record_gift_parse_failure_writes_recoverable_base64_payload(tmp_path):
    log_path = str(tmp_path / "gift_parse_failures.jsonl")
    payload = b"\n\xf6\x15\n\x12WebcastGiftMessage\x10\x87"

    with patch("tiktok_monitor.client.GIFT_PARSE_FAILURE_LOG_PATH", log_path):
        _record_gift_parse_failure(payload, username="baby_8_xo")

    with open(log_path, encoding="utf-8") as f:
        entry = json.loads(f.readline())

    assert entry["username"] == "baby_8_xo"
    assert entry["payload_bytes"] == len(payload)
    assert base64.b64decode(entry["payload_b64"]) == payload  # recoverable, not just a length/preview


def test_record_gift_parse_failure_logs_one_warning_line(tmp_path, caplog):
    log_path = str(tmp_path / "gift_parse_failures.jsonl")

    with patch("tiktok_monitor.client.GIFT_PARSE_FAILURE_LOG_PATH", log_path):
        with caplog.at_level("WARNING", logger="tiktok_monitor.client"):
            _record_gift_parse_failure(b"some bytes")

    messages = [r.message for r in caplog.records]
    assert any("gift parse failure" in m for m in messages)
    # Must not itself dump a payload preview/traceback into the log line --
    # the whole point is the noisy traceback stays suppressed and the data
    # lives in the recovery file instead.
    assert not any("Traceback" in m for m in messages)


def test_record_gift_parse_failure_handles_missing_payload(tmp_path):
    log_path = str(tmp_path / "gift_parse_failures.jsonl")

    with patch("tiktok_monitor.client.GIFT_PARSE_FAILURE_LOG_PATH", log_path):
        _record_gift_parse_failure(None)  # must not raise

    with open(log_path, encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["payload_bytes"] == 0
    assert entry["payload_b64"] is None


# --- _attach_gift_parse_failure_capture ------------------------------------


@patch("tiktok_monitor.client._record_gift_parse_failure")
def test_attach_captures_a_failed_gift_parse(mock_record):
    async def scenario():
        client = MagicMock()
        response_event = MagicMock()
        client._parse_webcast_response_message = MagicMock(return_value=_async_return([response_event]))
        _attach_gift_parse_failure_capture(client)

        message = make_response_message("WebcastGiftMessage", payload=b"the-raw-bytes")
        events = await client._parse_webcast_response_message(webcast_response=MagicMock(), webcast_response_message=message)

        assert events == [response_event]  # original result still returned, unchanged
        mock_record.assert_called_once_with(b"the-raw-bytes", username=None)

    asyncio.run(scenario())


@patch("tiktok_monitor.client._record_gift_parse_failure")
def test_attach_passes_the_streamer_username_through(mock_record):
    """So a mid-run Get-Content -Tail on gift_parse_failures.jsonl can show
    whose gift just got lost, not just when/how big."""

    async def scenario():
        client = MagicMock()
        response_event = MagicMock()
        client._parse_webcast_response_message = MagicMock(return_value=_async_return([response_event]))
        _attach_gift_parse_failure_capture(client, username="baby_8_xo")

        message = make_response_message("WebcastGiftMessage", payload=b"the-raw-bytes")
        await client._parse_webcast_response_message(webcast_response=MagicMock(), webcast_response_message=message)

        mock_record.assert_called_once_with(b"the-raw-bytes", username="baby_8_xo")

    asyncio.run(scenario())


@patch("tiktok_monitor.client._record_gift_parse_failure")
def test_attach_does_not_capture_a_successful_gift_parse(mock_record):
    async def scenario():
        client = MagicMock()
        response_event, gift_event = MagicMock(), MagicMock()
        client._parse_webcast_response_message = MagicMock(
            return_value=_async_return([response_event, gift_event])
        )
        _attach_gift_parse_failure_capture(client)

        message = make_response_message("WebcastGiftMessage")
        events = await client._parse_webcast_response_message(webcast_response=MagicMock(), webcast_response_message=message)

        assert events == [response_event, gift_event]  # untouched -- existing gift recording path unaffected
        mock_record.assert_not_called()

    asyncio.run(scenario())


@patch("tiktok_monitor.client._record_gift_parse_failure")
def test_attach_ignores_failures_for_other_message_types(mock_record):
    """Only WebcastGiftMessage is mitigated this way -- a WebcastLinkLayerMessage
    failure (e.g. the already-confirmed-harmless HashtagNamespace bug) isn't
    gift data and shouldn't be captured here."""

    async def scenario():
        client = MagicMock()
        response_event = MagicMock()
        client._parse_webcast_response_message = MagicMock(return_value=_async_return([response_event]))
        _attach_gift_parse_failure_capture(client)

        message = make_response_message("WebcastLinkLayerMessage")
        await client._parse_webcast_response_message(webcast_response=MagicMock(), webcast_response_message=message)

        mock_record.assert_not_called()

    asyncio.run(scenario())


async def _async_return(value):
    return value
