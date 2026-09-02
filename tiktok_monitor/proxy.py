"""Optional network proxy for TikTok connections (Phase 5 IP-based
measurement prep). Off by default -- with TTS_PROXY_URL unset, every
consumer here returns None and every code path connects on the real IP
exactly as before this module existed.

Only http:// proxy URLs are wired up for now (investigated against the
installed versions: TikTokLive 7.0.0, httpx 0.28.1, Playwright 1.62.0). This
is a deliberate scope choice, not a limitation of any one library --
http:// is the only scheme that works across all three consumers below with
zero extra installs:
  - web_proxy (TikTokLiveClient's httpx-based room-info/webcast API calls):
    http/https native; socks5 needs `pip install httpx[socks]` (socksio).
  - ws_proxy (TikTokLiveClient's WebSocket event stream -- comments/gifts):
    http/socks5/socks4 all work out of the box (via the websockets_proxy +
    python_socks packages TikTokLive already depends on), but NOT https.
  - Playwright's browser proxy (screenshot.py): http/https/socks5.
Add socks5 support later (httpx[socks] + passing scheme through unchanged)
if a measurement run specifically needs it; http covers every consumer
today without that extra dependency.

Credentials go directly in the URL: http://user:pass@host:port.

Signing IS proxied too, as of 2026-09-02 -- see client.py's
_route_signer_through_proxy(). TikTokLiveClient signs each webcast request
via a third-party service (api.eulerstream.com, "Euler Stream") rather than
TikTok's own servers. TikTokSigner.__init__ takes no proxy parameter, which
is why this used to say signing always went out on the real IP; but on
TikTokLive 7.0.0 the signer's underlying EulerApiSdk client exposes
set_async_httpx_client(), so a proxied httpx client can be injected after
construction.

That change matters because Euler Stream rate-limits **per IP**, not per
account (measured 2026-09-02 against /webcast/rate_limits: the VPS's own IP
read day 0/100 while all ten proxy IPs read day 100/100). Signing only from
the real IP capped the whole trial at 100 connections/day -- it was
exhausted by 19:30 and recording stopped for 2.5 hours. Routing each
recording's signing through the same proxy it records on spreads that
budget across every IP in the pool.

Limits observed per IP: day 100, hour 30, minute 5.
"""
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyConfig:
    url: str  # original URL, may include embedded user:pass
    host: str
    port: int
    username: str | None
    password: str | None

    @property
    def masked(self) -> str:
        """host:port only -- safe to log, never leaks credentials."""
        return f"{self.host}:{self.port}"


def parse_proxy_url(proxy_url: str) -> ProxyConfig:
    """Raises ValueError for anything not currently supported end-to-end
    (see module docstring) -- fail fast at startup rather than silently
    misconfiguring one of the three consumers."""
    parts = urlsplit(proxy_url)
    if parts.scheme != "http":
        raise ValueError(
            f"Unsupported proxy scheme {parts.scheme!r} in TTS_PROXY_URL -- only 'http://' is "
            "currently wired up (see tiktok_monitor/proxy.py's module docstring for why)"
        )
    if not parts.hostname or not parts.port:
        raise ValueError(f"TTS_PROXY_URL must include both host and port: {proxy_url!r}")
    return ProxyConfig(
        url=proxy_url,
        host=parts.hostname,
        port=parts.port,
        username=parts.username,
        password=parts.password,
    )


def build_httpx_proxy(config: ProxyConfig | None) -> httpx.Proxy | None:
    """Used for both TikTokLiveClient's web_proxy and ws_proxy -- it accepts
    the same httpx.Proxy object for both (ws_proxy converts it internally
    to a websockets_proxy.Proxy; see TikTokLive.client.ws.ws_connect)."""
    if config is None:
        return None
    return httpx.Proxy(url=config.url)


def build_playwright_proxy(config: ProxyConfig | None) -> dict | None:
    """Playwright's launch(proxy=...) wants credentials as separate fields,
    not embedded in the server URL (unlike httpx.Proxy)."""
    if config is None:
        return None
    proxy: dict = {"server": f"http://{config.host}:{config.port}"}
    if config.username is not None:
        proxy["username"] = config.username
    if config.password is not None:
        proxy["password"] = config.password
    return proxy


def load_proxy_config(proxy_url: str | None) -> ProxyConfig | None:
    """Parses (and logs) the configured proxy, or logs that none is set --
    call once per connection attempt so a run's logs always show which IP
    path was actually used, which is the whole point during Phase 5
    measurement. Not logged: credentials (see ProxyConfig.masked)."""
    if not proxy_url:
        logger.info("no proxy configured, connecting with the real IP")
        return None
    config = parse_proxy_url(proxy_url)
    logger.info("using proxy %s for web_proxy/ws_proxy/screenshot", config.masked)
    return config
