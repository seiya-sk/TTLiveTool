import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.proxy import (
    build_httpx_proxy,
    build_playwright_proxy,
    load_proxy_config,
    parse_proxy_url,
)


def test_parse_proxy_url_extracts_host_port_and_credentials():
    config = parse_proxy_url("http://myuser:mypass@proxy.example.com:8080")
    assert config.host == "proxy.example.com"
    assert config.port == 8080
    assert config.username == "myuser"
    assert config.password == "mypass"
    assert config.url == "http://myuser:mypass@proxy.example.com:8080"


def test_parse_proxy_url_without_credentials():
    config = parse_proxy_url("http://proxy.example.com:8080")
    assert config.username is None
    assert config.password is None


def test_parse_proxy_url_rejects_socks5_scheme():
    # Not wired up yet -- see proxy.py's module docstring for why.
    with pytest.raises(ValueError, match="Unsupported proxy scheme"):
        parse_proxy_url("socks5://proxy.example.com:1080")


def test_parse_proxy_url_rejects_missing_port():
    with pytest.raises(ValueError, match="host and port"):
        parse_proxy_url("http://proxy.example.com")


def test_masked_never_includes_credentials():
    config = parse_proxy_url("http://secretuser:secretpass@proxy.example.com:8080")
    assert "secretuser" not in config.masked
    assert "secretpass" not in config.masked
    assert config.masked == "proxy.example.com:8080"


def test_build_httpx_proxy_returns_none_for_none_config():
    assert build_httpx_proxy(None) is None


def test_build_httpx_proxy_wraps_the_url():
    config = parse_proxy_url("http://user:pass@proxy.example.com:8080")
    proxy = build_httpx_proxy(config)
    assert isinstance(proxy, httpx.Proxy)
    # httpx.Proxy splits credentials out of .url into .auth on its own.
    assert str(proxy.url) == "http://proxy.example.com:8080"
    assert proxy.auth == ("user", "pass")


def test_build_playwright_proxy_returns_none_for_none_config():
    assert build_playwright_proxy(None) is None


def test_build_playwright_proxy_puts_credentials_in_separate_fields():
    config = parse_proxy_url("http://user:pass@proxy.example.com:8080")
    proxy = build_playwright_proxy(config)
    assert proxy == {
        "server": "http://proxy.example.com:8080",
        "username": "user",
        "password": "pass",
    }
    assert "user" not in proxy["server"]
    assert "pass" not in proxy["server"]


def test_build_playwright_proxy_omits_credential_fields_when_absent():
    config = parse_proxy_url("http://proxy.example.com:8080")
    proxy = build_playwright_proxy(config)
    assert proxy == {"server": "http://proxy.example.com:8080"}


def test_load_proxy_config_returns_none_for_unset():
    assert load_proxy_config(None) is None
    assert load_proxy_config("") is None


def test_load_proxy_config_parses_a_set_url():
    config = load_proxy_config("http://user:pass@proxy.example.com:8080")
    assert config is not None
    assert config.masked == "proxy.example.com:8080"
