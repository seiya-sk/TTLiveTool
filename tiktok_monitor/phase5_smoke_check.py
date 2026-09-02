"""Phase 5 preflight: a single, isolated check_is_live call through the
configured proxy. Exists specifically so a live smoke test never needs to
guess whether TikTok's response was "not live" or "blocked" -- unlike
watch.check_is_live (which deliberately swallows all failures as "not
live", correct for its own continuous-polling use case), this calls the
same underlying route directly and surfaces the raw exception, including
an HTTP status code if TikTok returned one (e.g. 403).

Refuses to run at all if TTS_PROXY_URL is unset, rather than silently
falling back to the real IP -- see docs/phase5-1ip-measurement-spec.md and
tiktok_monitor/proxy.py's module docstring for why real-IP fallback during
Phase 5 measurement must never happen silently.
"""
import argparse
import asyncio
import logging
import os
import sys

import httpx
from TikTokLive.client.web.web_client import TikTokWebClient

from . import proxy as proxy_module

logger = logging.getLogger(__name__)


async def _run(username: str) -> int:
    proxy_url = os.environ.get("TTS_PROXY_URL")
    if not proxy_url:
        logger.error("TTS_PROXY_URL is not set -- refusing to run (would silently fall back to the real IP)")
        return 1

    proxy_config = proxy_module.load_proxy_config(proxy_url)  # logs "using proxy host:port"
    proxy = proxy_module.build_httpx_proxy(proxy_config)

    web = TikTokWebClient(web_proxy=proxy)
    try:
        is_live = await web.fetch_is_live(unique_id=username)
    except httpx.HTTPStatusError as exc:
        logger.error("check_is_live(@%s) FAILED with HTTP %s: %s", username, exc.response.status_code, exc)
        return 1
    except Exception as exc:
        logger.error("check_is_live(@%s) FAILED: %r", username, exc)
        return 1

    logger.info("check_is_live(@%s) OK -> is_live=%s", username, is_live)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("username", help="Any TikTok username -- live status doesn't matter, only whether the request itself succeeds")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.username)))


if __name__ == "__main__":
    main()
