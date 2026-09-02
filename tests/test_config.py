import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.config import Settings


def test_proxy_url_defaults_to_none():
    settings = Settings(username="someone")
    assert settings.proxy_url is None


def test_from_args_leaves_proxy_url_none_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("TTS_PROXY_URL", raising=False)
    settings = Settings.from_args("someone", None, None)
    assert settings.proxy_url is None


def test_from_args_reads_proxy_url_from_env(monkeypatch):
    monkeypatch.setenv("TTS_PROXY_URL", "http://user:pass@proxy.example.com:8080")
    settings = Settings.from_args("someone", None, None)
    assert settings.proxy_url == "http://user:pass@proxy.example.com:8080"


def test_from_args_treats_blank_proxy_url_env_as_unset(monkeypatch):
    monkeypatch.setenv("TTS_PROXY_URL", "")
    settings = Settings.from_args("someone", None, None)
    assert settings.proxy_url is None


def test_screenshots_enabled_defaults_to_true():
    settings = Settings(username="someone")
    assert settings.screenshots_enabled is True


def test_from_args_leaves_screenshots_enabled_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("TTS_DISABLE_SCREENSHOTS", raising=False)
    settings = Settings.from_args("someone", None, None)
    assert settings.screenshots_enabled is True


def test_from_args_disables_screenshots_when_env_var_set(monkeypatch):
    monkeypatch.setenv("TTS_DISABLE_SCREENSHOTS", "1")
    settings = Settings.from_args("someone", None, None)
    assert settings.screenshots_enabled is False


def test_from_args_fails_fast_on_an_unsupported_proxy_scheme(monkeypatch):
    # A typo'd TTS_PROXY_URL should surface immediately at startup, not get
    # silently swallowed by run_with_reconnect's broad retry-on-Exception
    # loop later.
    monkeypatch.setenv("TTS_PROXY_URL", "socks5://proxy.example.com:1080")
    with pytest.raises(ValueError, match="Unsupported proxy scheme"):
        Settings.from_args("someone", None, None)
