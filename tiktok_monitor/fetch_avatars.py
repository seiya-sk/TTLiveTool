"""Streamer avatar fetch + local cache.

The dashboard's POST /api/streamers route shells out to this module
(`python -m tiktok_monitor.fetch_avatars <tiktok_account_id>`) right after
registering a new streamer, the same way fx-rate/route.ts shells out to
tiktok_monitor.fxrate -- so a newly-added streamer's icon appears without
the user doing anything extra. Run with no positional args to (re)fetch
every non-archived streamer at once, e.g. to backfill icons for streamers
registered before this existed:

    python -m tiktok_monitor.fetch_avatars
    python -m tiktok_monitor.fetch_avatars chanhika825 bufferin05

TikTok's /room/info_by_user/ endpoint (what TikTokLive's fetch_room_info
wraps) needs no signing/sign-server key -- it's a plain unsigned GET, unlike
the websocket connection -- but it only returns the streamer's profile data
(including avatar) while their room is still resolvable server-side.
Confirmed empirically: for streamers not CURRENTLY live, this often comes
back as `{"message": "room has finished", "prompts": "..."}` with no owner
data at all, and TikTokLive's own route wrapper misreports that specific
case as AgeRestrictedError (its `"prompts" in data` check also fires for
this ordinary "not live" response, not just genuine age gates) -- so this
module calls the endpoint directly instead of going through
client.web.fetch_room_info(), to get an accurate "no data available" signal
rather than a misleading age-restriction error. See client.py's
_maybe_fetch_avatar for the one place this is guaranteed to succeed
(mid-connect, when the room is definitely resolvable).

The avatar CDN URL IS signed (x-expires=... query param), but confirmed
against real accounts to be valid for months, far longer than the ~47-hour
expiry seen on per-event (comment/gift) avatar URLs -- still, this
downloads and caches the image bytes locally rather than storing the URL,
so the dashboard never depends on that URL staying valid or on TikTok's CDN
being reachable on every page load.
"""
import argparse
import asyncio
import logging
import os
import sqlite3
import urllib.request
from urllib.error import URLError

import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive.client.web.web_settings import WebDefaults

from . import db

logger = logging.getLogger(__name__)

DEFAULT_AVATAR_DIR = "data/avatars"
# TikTok's avatar CDN has consistently served WebP for every account
# sampled during development -- if that ever changes, downloads would
# still succeed, just mislabeled by extension. Not worth sniffing
# content-type for a one-image-per-streamer fetch.
AVATAR_EXT = "webp"

ROOM_INFO_URL = WebDefaults.tiktok_webcast_url + "/room/info_by_user/"


def default_avatar_path(base_dir: str, tiktok_account_id: str) -> str:
    return os.path.join(base_dir, f"{tiktok_account_id}.{AVATAR_EXT}")


def _extract_avatar_url(room_info: dict) -> str | None:
    """Pulls the first avatar_medium CDN URL out of a room-info response's
    "data" dict. Separated from the network call so this shape-parsing
    logic can be unit tested against a captured payload without hitting
    the network (mirrors fxrate.py's _parse_rate)."""
    owner = room_info.get("owner") or {}
    urls = (owner.get("avatar_medium") or {}).get("url_list") or []
    return urls[0] if urls else None


async def fetch_avatar_url(unique_id: str) -> str | None:
    """Returns None (not an exception) when the room simply isn't
    resolvable right now -- see module docstring for why that's common and
    not actually an error. Raises httpx.HTTPError on genuine network/HTTP
    failure -- callers decide whether that's fatal for their run."""
    client = TikTokLiveClient(unique_id=unique_id)
    response = await client.web.get(url=ROOM_INFO_URL, extra_params={"unique_id": unique_id})
    room_info = response.json().get("data") or {}
    return _extract_avatar_url(room_info)


def download_avatar(url: str, dest_path: str, timeout: float = 15.0) -> None:
    directory = os.path.dirname(dest_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # TikTok's CDN 403s the default urllib User-Agent, same as the fx-rate
    # API (see fxrate.py) -- any non-empty override works.
    request = urllib.request.Request(url, headers={"User-Agent": "TTSLiveTool/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        body = resp.read()
    with open(dest_path, "wb") as f:
        f.write(body)


async def fetch_and_store_avatar(
    conn: sqlite3.Connection, streamer_id: int, tiktok_account_id: str, avatar_dir: str
) -> bool:
    """Returns True on success, False if the account simply has no avatar
    to fetch. Network/parse failures propagate to the caller, which
    decides per-account whether to keep going with the rest of the batch."""
    url = await fetch_avatar_url(tiktok_account_id)
    if not url:
        logger.warning(
            "no avatar available for @%s (room not currently resolvable -- "
            "streamer may never have gone live yet, or hasn't recently enough)",
            tiktok_account_id,
        )
        return False
    dest_path = default_avatar_path(avatar_dir, tiktok_account_id)
    download_avatar(url, dest_path)
    db.set_streamer_avatar_path(conn, streamer_id, dest_path)
    logger.info("avatar saved for @%s: %s", tiktok_account_id, dest_path)
    return True


async def run(conn: sqlite3.Connection, avatar_dir: str, account_ids: list[str] | None) -> None:
    if account_ids:
        targets = []
        for account_id in account_ids:
            row = conn.execute(
                "SELECT id, tiktok_account_id FROM streamers WHERE tiktok_account_id = ?", (account_id,)
            ).fetchone()
            if row is None:
                logger.error("no registered streamer with tiktok_account_id=%s", account_id)
                continue
            targets.append(row)
    else:
        targets = conn.execute("SELECT id, tiktok_account_id FROM streamers WHERE archived = 0").fetchall()

    for streamer_id, tiktok_account_id in targets:
        try:
            await fetch_and_store_avatar(conn, streamer_id, tiktok_account_id, avatar_dir)
        except (httpx.HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.error("avatar fetch failed for @%s: %s", tiktok_account_id, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and cache streamer avatar images.")
    parser.add_argument("--db-path", default="data/tts_live_tool.db")
    parser.add_argument("--avatar-dir", default=DEFAULT_AVATAR_DIR)
    parser.add_argument(
        "account_ids",
        nargs="*",
        help="Specific tiktok_account_id(s) to fetch; all non-archived streamers if omitted.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    conn = db.connect(args.db_path)
    db.init_schema(conn)
    try:
        asyncio.run(run(conn, args.avatar_dir, args.account_ids or None))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
