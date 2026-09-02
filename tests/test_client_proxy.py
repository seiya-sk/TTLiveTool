import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner
from tiktok_monitor.config import Settings

# Exercises SessionRunner.build_client()'s proxy wiring without connecting to
# TikTok: TikTokLiveClient itself is mocked out (its constructor does no
# network I/O -- confirmed by reading TikTokLive's source -- but mocking it
# here keeps this test from depending on that staying true, and avoids the
# listener-registration loop needing a real client). Only the constructor
# call's kwargs are asserted; add_listener/on calls just need to not raise
# against a MagicMock.


def make_runner(**settings_kwargs):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(username="target_streamer", db_path=":memory:", idle_timeout_sec=60, **settings_kwargs)
    runner = SessionRunner(conn, settings)
    return runner, conn


@patch("tiktok_monitor.client.TikTokLiveClient")
def test_build_client_passes_no_proxy_when_unconfigured(mock_client_cls):
    mock_client_cls.return_value = MagicMock()
    runner, _conn = make_runner()  # proxy_url defaults to None

    runner.build_client()

    _args, kwargs = mock_client_cls.call_args
    assert kwargs["unique_id"] == "target_streamer"
    assert kwargs["web_proxy"] is None
    assert kwargs["ws_proxy"] is None


@patch("tiktok_monitor.client.TikTokLiveClient")
def test_build_client_passes_the_same_proxy_to_web_and_ws(mock_client_cls):
    mock_client_cls.return_value = MagicMock()
    runner, _conn = make_runner(proxy_url="http://user:pass@proxy.example.com:8080")

    runner.build_client()

    _args, kwargs = mock_client_cls.call_args
    web_proxy = kwargs["web_proxy"]
    ws_proxy = kwargs["ws_proxy"]
    assert isinstance(web_proxy, httpx.Proxy)
    assert isinstance(ws_proxy, httpx.Proxy)
    assert str(web_proxy.url) == "http://proxy.example.com:8080"
    assert web_proxy.auth == ("user", "pass")
    assert str(ws_proxy.url) == str(web_proxy.url)
    assert ws_proxy.auth == web_proxy.auth


@patch("tiktok_monitor.client.TikTokLiveClient")
def test_build_client_rejects_unsupported_proxy_scheme(mock_client_cls):
    mock_client_cls.return_value = MagicMock()
    runner, _conn = make_runner(proxy_url="socks5://proxy.example.com:1080")

    try:
        runner.build_client()
        assert False, "expected ValueError for an unsupported proxy scheme"
    except ValueError as exc:
        assert "Unsupported proxy scheme" in str(exc)
