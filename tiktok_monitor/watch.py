"""Persistent watcher: polls a list of registered streamers for live status
and automatically starts recording (the existing SessionRunner/
run_with_reconnect machinery from client.py) as soon as one goes live, then
returns to polling once that session ends. Reference pattern: sample/
try3live.py's round-robin loop ("🔄 メイン巡回ループ"), reimplemented on top
of this project's asyncio-based, reconnect-aware recording path instead of
the reference script's blocking client.run().

Watch list source (2026-08-29 dashboard-integration): with no usernames on
the command line, the watched set is read from the streamers table
(archived=0) and re-read at the start of every poll cycle -- adding or
archiving a streamer in the dashboard's ライバー管理 screen takes effect
within one poll interval, no restart needed. Archiving only stops NEW
recordings from starting for that streamer; a session already in progress
when it's archived is left to end naturally (stream end or idle timeout) --
forcibly cutting off an in-progress recording is out of scope until
explicitly needed. Passing usernames explicitly overrides the DB entirely
(fixed for the process's lifetime, DB not re-read) -- an escape hatch for
testing/partial runs without touching the dashboard's data.

Single-IP constraint (design doc 5.4): only one stream can be recorded at a
time until multi-IP support exists (Phase 5). If more than one watched
streamer is live in the same poll cycle, the FIRST one found (in list
order -- command-line order when overridden, otherwise
list_streamers' archived/name order) wins; the others are simply
re-checked on the next cycle once the current recording ends. No queue or
priority beyond list order -- that's out of scope until multiple IPs make
concurrent recording possible.

Proxy required (2026-08-29): refuses to start at all unless TTS_PROXY_URL is
set, in either mode -- see require_proxy_or_exit. Pass --allow-no-proxy to
intentionally bypass this for local testing.

Concurrent-live measurement + check-pacing (2026-08-29): a second, fully
independent task (concurrency_poll_loop) runs alongside watch_loop (see
main()'s asyncio.gather) and logs how many watched streamers are live at
each sweep to data/concurrent_live.jsonl -- real-ops IP-pool sizing needs
an actual measured peak concurrency, not a guess. It has to be a SEPARATE
task, not folded into watch_loop's own scan: watch_loop blocks on
run_with_reconnect for the entire duration of an active recording, so
measurement folded into that loop would go blind for hours at exactly the
periods most likely to contain the peak (whenever someone is already
live). Both this loop's and watch_loop's check_is_live calls are routed
through one shared CheckPacer instance, capping the COMBINED rate across
both loops to one check every CHECK_PACE_SEC regardless of pool size --
mirrors phase5_measure.py's Orchestrator._paced_check_is_live /
POOL_CHECK_INTERVAL_SEC, the same empirically-confirmed safe rate (firing
10 concurrent check_is_live calls via asyncio.gather drew a 403 from
TikTok that an isolated single request did not). At real-ops scale
(dozens of streamers) a full paced sweep still finishes within a poll
interval or two; at hypothetical 200-streamer scale one sweep takes
~pool_size * CHECK_PACE_SEC (~17 minutes) -- concurrency numbers become an
approximation over that rolling window rather than an instant snapshot,
and new-streamer detection latency grows to match. That's an intentional
trade (safety over precision/speed) consistent with this project's
always-via-proxy/zero-burst rule, not something to fix by relaxing the
pace.

Report generation stays a separate manual step (tiktok_monitor.generate_report);
this process never calls the Claude API.
"""
import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable

import httpx
from TikTokLive.client.errors import UserNotFoundError, UserOfflineError
from TikTokLive.client.web.web_client import TikTokWebClient

from . import db
from . import cleanup_raw_payloads as cleanup_module
from . import proxy as proxy_module
from .client import SessionRunner, run_with_reconnect
from .config import Settings

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/tts_live_tool.db"
DEFAULT_POLL_INTERVAL_SEC = 60.0
DEFAULT_CONCURRENCY_LOG_PATH = "data/concurrent_live.jsonl"
POST_SESSION_COOLDOWN_SEC = 15.0  # brief pause before resuming the scan, mirrors sample/try3live.py
RAW_PAYLOAD_CLEANUP_INTERVAL_SEC = 24 * 3600  # "1日1回程度" -- cheap to check, no need to run more often
# Same value and justification as phase5_measure.py's POOL_CHECK_INTERVAL_SEC
# (see that module's docstring for the empirical 403 finding) -- not
# duplicated by import since the two modules are otherwise independent CLI
# entry points, but the underlying TikTok-side constraint is identical.
CHECK_PACE_SEC = 5.0


async def check_is_live(
    username: str,
    web_proxy: httpx.Proxy | None = None,
    on_error: "Callable[[Exception], None] | None" = None,
) -> bool:
    """Lightweight live-status probe (no websocket connection). Any failure
    (network hiccup, transient block, user genuinely offline/not found) is
    treated as "not live for now" -- the next poll cycle tries again, so a
    single flaky check never brings the watcher down.

    A single call to fetch_is_live(unique_id=...) -- not two. An earlier
    version of this function first resolved a room_id via
    fetch_room_id_from_html (a full page-HTML fetch + regex parse of the
    embedded SIGI_STATE JSON) and then passed that room_id to fetch_is_live.
    But FetchIsLiveRoute accepts unique_id directly and, when given one,
    calls TikTok's much lighter /api-live/user/room/ JSON endpoint itself
    (see TikTokLive.client.web.routes.fetch_is_live.FetchIsLiveRoute) --
    same anonymous, unauthenticated check, half the HTTP requests per poll.

    on_error (2026-08-31, proxy_pool_trial.py's rate-limit probing) is called
    with the swallowed exception, if any, before returning False -- this
    function's own return value never distinguishes "genuinely offline" from
    "blocked/rate-limited", but a caller that needs to tell those apart for
    logging (e.g. detecting which proxy IP got a 403 during an aggressive
    scan) can via this hook. None (the default) preserves the original
    silent-swallow behavior for watch.py/phase5_measure.py.

    web_proxy is optional (Phase 5 IP-based measurement prep) -- None
    connects with the real IP exactly as before this parameter existed.
    In multi-streamer watch mode this check runs every poll cycle for every
    registered streamer, so it must go through the same proxy as the actual
    recording connection (client.py's build_client) -- otherwise this probe
    alone would keep leaking the real IP to TikTok even with a proxy
    configured, defeating the point of IP-based measurement.

    Never touches URL signing (sign_url defaults to False throughout
    TikTokHTTPClient/FetchIsLiveRoute) -- confirmed by reading the
    installed library -- so this consumes no Euler Stream quota, unlike
    actually starting a recording. Relevant for --measure-only: measuring
    concurrency across a large population never touches the signing budget
    that real recordings depend on.

    2026-08-29 (528-account concurrency measurement prep): explicitly
    closes its TikTokWebClient/httpx.AsyncClient every call instead of
    relying on garbage collection. A fresh client was already created per
    call before this; at small scale (a handful of streamers) an unclosed
    client per call was never enough volume to notice, but a large
    population swept continuously for weeks (tens of thousands of calls/
    day) is exactly the regime where relying on GC for async resource
    cleanup becomes a real, not just theoretical, risk."""
    return await check_live_status(username, web_proxy=web_proxy, on_error=on_error) == LIVE


# 生存確認の3値。bool だと「オフライン」と「確認できなかった」が区別できない。
LIVE = "live"
OFFLINE = "offline"
UNKNOWN = "unknown"

# 「確実にオフライン」と言い切れる例外。アカウントが存在しない/配信していない
# という TikTok 側の明確な回答なので、ネットワーク障害とは性質が違う。
_DEFINITE_OFFLINE_ERRORS = (UserNotFoundError, UserOfflineError)


async def check_live_status(
    username: str,
    web_proxy: httpx.Proxy | None = None,
    on_error: "Callable[[Exception], None] | None" = None,
) -> str:
    """LIVE / OFFLINE / UNKNOWN を返す生存確認。

    **この確認は Euler Stream の署名を一切消費しない。** 巡回で1日数千回
    呼ばれるので、ここが署名を使っていたら即座に日次上限を超える。
    根拠2点(2026-09-02 確認):
      - コード: TikTokLive の FetchIsLiveRoute は self._web.get() で
        TikTok の /api-live/user/room/ を直接叩くだけで、self._web.signer
        には触れない(署名を使うのは fetch_signed_websocket だけ)。
      - 実測: プロキシ経由で10回連続実行した前後で
        /webcast/rate_limits の day/hour/minute のどれも減らなかった。
    したがって「確認は無料、繋ぎ直しは有料」という非対称性が成立する。
    生存確認は積極的に行い、再接続だけを絞るのが正しい設計になる。

    check_is_live() は歴史的に bool を返し、**あらゆる失敗を False(=オフライン)
    に丸めていた**。巡回で使う分には「次の周期で再試行するだけ」なので害が
    無かったが、proxy_pool_trial の生存確認は False を「配信が終わった」と
    解釈してセッションを終了させる。そこでネットワークが一瞬詰まっただけで
    録画中のセッションを誤って閉じてしまう。

    実測(2026-09-02): 巡回チェックの is_live=False 4,712件のうち 140件
    (3.0%)は、同時刻にタイムアウト等のエラーが記録されていた。つまり
    「オフライン」ではなく「確認できなかった」もの。生存確認でこれを引くと
    誤終了になる。

    UserNotFoundError / UserOfflineError だけは OFFLINE として扱う --
    これらは TikTok からの明確な回答であって、通信の失敗ではない。
    """
    web = TikTokWebClient(web_proxy=web_proxy)
    try:
        return LIVE if await web.fetch_is_live(unique_id=username) else OFFLINE
    except _DEFINITE_OFFLINE_ERRORS as exc:
        logger.debug("live check: @%s is definitively offline: %s", username, exc)
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                logger.debug("on_error callback itself raised, ignoring", exc_info=True)
        return OFFLINE
    except Exception as exc:
        logger.debug("live check failed for @%s: %s", username, exc)
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                logger.debug("on_error callback itself raised, ignoring", exc_info=True)
        return UNKNOWN
    finally:
        try:
            await web.close()
        except Exception:
            pass  # best-effort cleanup; must never mask the check's own result


class CheckPacer:
    """Serializes check_is_live calls to at least pace_sec apart, globally
    -- shared by watch_loop and concurrency_poll_loop so the COMBINED rate
    across both is capped, not just each independently. Mirrors
    phase5_measure.py's Orchestrator._paced_check_is_live (see this
    module's docstring for why the rate matters). pace_sec is injectable
    (production uses CHECK_PACE_SEC; tests use 0.0) so exercising a
    multi-username sweep in a test doesn't mean actually waiting out real
    5-second gaps."""

    def __init__(self, pace_sec: float = CHECK_PACE_SEC):
        self.pace_sec = pace_sec
        self._lock = asyncio.Lock()
        self._last_check_at = 0.0

    async def check(
        self,
        username: str,
        web_proxy: httpx.Proxy | None = None,
        on_error: "Callable[[Exception], None] | None" = None,
    ) -> bool:
        async with self._lock:
            now = time.monotonic()
            wait = self.pace_sec - (now - self._last_check_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_check_at = time.monotonic()
            return await check_is_live(username, web_proxy=web_proxy, on_error=on_error)

    async def check_status(
        self,
        username: str,
        web_proxy: httpx.Proxy | None = None,
        on_error: "Callable[[Exception], None] | None" = None,
    ) -> str:
        """check() の3値版。LIVE / OFFLINE / UNKNOWN。同じペース制限を通る。"""
        async with self._lock:
            now = time.monotonic()
            wait = self.pace_sec - (now - self._last_check_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_check_at = time.monotonic()
            return await check_live_status(username, web_proxy=web_proxy, on_error=on_error)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: str, obj: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read_usernames_file(path: str) -> list[str]:
    """One username per line; blank lines and #-comments ignored;
    duplicates collapsed (first occurrence wins, preserving order).
    Mirrors phase5_measure.py's load_pool -- duplicated rather than
    imported to avoid a circular import (phase5_measure already imports
    check_is_live from this module)."""
    usernames: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if not name or name.startswith("#"):
                continue
            if name not in seen:
                seen.add(name)
                usernames.append(name)
    return usernames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch one or more streamers and auto-start recording when they go live."
    )
    parser.add_argument(
        "usernames",
        nargs="*",
        help=(
            "TikTok unique_id(s) to watch (without @). Omit to watch every "
            "non-archived streamer registered in the dashboard instead "
            "(streamers table, archived=0) -- re-read every poll cycle, so "
            "additions/archives there take effect without restarting this "
            "process. If given, watches only these usernames instead, fixed "
            "for this run (the DB's streamer list is never consulted) -- "
            "for testing/partial runs."
        ),
    )
    parser.add_argument(
        "--usernames-file",
        default=None,
        help=(
            "Path to a file with one TikTok unique_id per line (blank lines and #-comments "
            "ignored; see phase5_measure.py's load_pool for the same format). Merged into the "
            "override list alongside any positional usernames. For measuring concurrency across "
            "a large ad-hoc population (e.g. sample/streamers.txt) without registering them "
            "as real streamers in the dashboard's DB -- pair with --measure-only."
        ),
    )
    parser.add_argument(
        "--measure-only",
        action="store_true",
        help=(
            "Run only concurrency_poll_loop -- no recording-trigger watch_loop, no SessionRunner, "
            "no recordings started, no Euler Stream signing consumed (check_is_live never signs a "
            "URL; only starting an actual recording does). For measuring concurrent-live counts "
            "across a population you don't intend to record, e.g. a large candidate pool via "
            "--usernames-file, without side effects on the dashboard's streamers table or storage."
        ),
    )
    parser.add_argument("--db-path", default=None, help="SQLite DB file path (default: data/tts_live_tool.db)")
    parser.add_argument(
        "--idle-timeout", type=float, default=None, help="Seconds with no events before auto-ending a session (default: 60)"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SEC,
        help=f"Seconds between live-status checks while idle (default: {DEFAULT_POLL_INTERVAL_SEC:.0f})",
    )
    parser.add_argument(
        "--concurrency-log-path",
        default=DEFAULT_CONCURRENCY_LOG_PATH,
        help=(
            f"jsonl file logging how many watched streamers were live at each sweep, for measuring "
            f"real peak concurrency / IP-pool sizing (default: {DEFAULT_CONCURRENCY_LOG_PATH})"
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--allow-no-proxy",
        action="store_true",
        help=(
            "Allow starting without TTS_PROXY_URL set, connecting to TikTok on this machine's "
            "real IP. Refused by default (see require_proxy_or_exit) -- this project's rule is "
            "always-via-proxy for real connections, and relying on a human to remember to set "
            "TTS_PROXY_URL every run has already caused accidental real-IP recordings. Only for "
            "intentional local testing."
        ),
    )
    return parser.parse_args()


def _maybe_cleanup_raw_payloads(conn, last_cleanup_at: float, now: float) -> float:
    """Runs the raw_payload retention cleanup at most once per
    RAW_PAYLOAD_CLEANUP_INTERVAL_SEC, returning the (possibly updated)
    last-run timestamp. A cleanup failure is logged and swallowed -- it
    must never take down the watcher -- and still counts as "ran" so a
    persistent failure retries once a day rather than every poll cycle."""
    if now - last_cleanup_at < RAW_PAYLOAD_CLEANUP_INTERVAL_SEC:
        return last_cleanup_at

    try:
        result = cleanup_module.cleanup_raw_payloads(conn)
        if result["sessions"]:
            logger.info(
                "raw_payload cleanup: retention=%d day(s), %d session(s), %d row(s) deleted",
                result["retention_days"],
                result["sessions"],
                result["rows"],
            )
        else:
            logger.debug("raw_payload cleanup: nothing eligible (retention=%d day(s))", result["retention_days"])
    except Exception:
        logger.warning("raw_payload cleanup failed", exc_info=True)

    return now


def resolve_usernames(conn, override: list[str] | None) -> list[str]:
    """override non-empty -> use it as-is, fixed for the run (explicit
    command-line usernames; the DB is never consulted in this mode).
    override empty/None -> every non-archived streamer, read fresh from the
    DB every call. Called once per poll cycle from watch_loop so an
    addition/archive in the dashboard takes effect within one cycle without
    restarting this process."""
    if override:
        return override
    return [s["tiktok_account_id"] for s in db.list_streamers(conn, include_archived=False)]


async def watch_loop(
    conn,
    usernames_override: list[str] | None,
    settings_kwargs: dict,
    poll_interval: float,
    active: dict,
    pacer: "CheckPacer",
    web_proxy: httpx.Proxy | None = None,
) -> None:
    """Recording-trigger scan only -- see concurrency_poll_loop (run
    concurrently via main()'s asyncio.gather) for the independent
    always-on concurrent-live measurement. `pacer` must be the SAME
    CheckPacer instance passed to concurrency_poll_loop so the two loops'
    check_is_live calls share one global rate limit."""
    last_cleanup_at = 0.0  # 0 so the first loop iteration runs an initial cleanup check
    while True:
        last_cleanup_at = _maybe_cleanup_raw_payloads(conn, last_cleanup_at, time.time())

        usernames = resolve_usernames(conn, usernames_override)
        recorded_this_cycle = False

        for username in usernames:
            if not await pacer.check(username, web_proxy=web_proxy):
                continue

            logger.info("@%s is live -- starting recording", username)
            settings = Settings.from_args(username, settings_kwargs["db_path"], settings_kwargs["idle_timeout"])
            runner = SessionRunner(conn, settings)
            active["runner"] = runner
            try:
                await run_with_reconnect(runner, settings)
            finally:
                if not runner.ended:
                    runner.manual_end()
                active["runner"] = None
            logger.info("recording for @%s ended -- resuming watch", username)

            recorded_this_cycle = True
            break  # re-scan the full list (in order) next cycle rather than continuing mid-list

        await asyncio.sleep(POST_SESSION_COOLDOWN_SEC if recorded_this_cycle else poll_interval)


async def concurrency_poll_loop(
    conn,
    usernames_override: list[str] | None,
    pacer: "CheckPacer",
    poll_interval: float,
    concurrency_path: str,
    web_proxy: httpx.Proxy | None = None,
) -> None:
    """Independent of watch_loop's recording-trigger scan -- runs
    concurrently (see main()'s asyncio.gather) so it keeps measuring even
    while watch_loop is blocked recording an active stream; folding this
    into watch_loop's own scan would go blind for the entire duration of
    every recording, exactly when peak concurrency is most likely. Sharing
    `pacer` with watch_loop caps the COMBINED check_is_live rate across
    both loops, not each independently -- see CheckPacer and this module's
    docstring.

    One jsonl line per completed sweep of the current streamer list, not
    strictly one per poll_interval: at real-ops scale a sweep finishes
    within poll_interval, but a large pool paced at CHECK_PACE_SEC can take
    much longer than poll_interval to sweep once -- see the module
    docstring for why that's an intentional safety/precision trade, not a
    bug to work around."""
    while True:
        sweep_started_at = time.monotonic()
        try:
            usernames = resolve_usernames(conn, usernames_override)
            live_now = [u for u in usernames if await pacer.check(u, web_proxy=web_proxy)]

            _append_jsonl(
                concurrency_path,
                {
                    "timestamp": _now_iso(),
                    "checked_count": len(usernames),
                    "live_count": len(live_now),
                    "live_usernames": live_now,
                },
            )
            if live_now:
                logger.info("concurrent live: %d/%d live %s", len(live_now), len(usernames), live_now)
        except Exception:
            # Never let a logging/DB hiccup here take down the watcher --
            # same reasoning as _maybe_cleanup_raw_payloads.
            logger.warning("concurrency poll sweep failed, will retry next cycle", exc_info=True)

        elapsed = time.monotonic() - sweep_started_at
        await asyncio.sleep(max(0.0, poll_interval - elapsed))


async def _run_watch_and_concurrency_poll(watch_coro, concurrency_coro) -> None:
    """asyncio.run() requires an actual coroutine, not the Future-like
    object asyncio.gather() returns directly -- this thin wrapper is that
    coroutine. If either task raises unexpectedly, gather cancels the
    other and re-raises here (main()'s existing KeyboardInterrupt/finally
    handling around asyncio.run() is unaffected)."""
    await asyncio.gather(watch_coro, concurrency_coro)


def require_proxy_or_exit(proxy_url: str | None, allow_no_proxy: bool, mode_desc: str) -> None:
    """2026-08-29 safety net: two separate real-IP accidental recordings
    during testing (a human forgetting to set TTS_PROXY_URL is not a
    reliable enough safeguard) established that this must be enforced in
    code, not left to operator attention. Applies uniformly whether the
    watch list comes from the DB (real-ops mode) or an explicit
    command-line override (test/partial-run mode) -- the real-IP risk
    comes from the missing proxy itself, not from which mode started the
    process, and the incident that prompted this happened during a test
    run. Called before any DB access so a misconfigured run fails as early
    as possible."""
    if proxy_url:
        return
    if not allow_no_proxy:
        logger.error(
            "TTS_PROXY_URL is not set (%s). Refusing to start on the real IP -- this project's "
            "rule is always-via-proxy for real connections. Set TTS_PROXY_URL, or pass "
            "--allow-no-proxy to intentionally run on the real IP (e.g. local testing).",
            mode_desc,
        )
        raise SystemExit(1)
    logger.warning(
        "--allow-no-proxy given with no TTS_PROXY_URL: CONNECTING WITH THE REAL IP (%s). "
        "Do not use this for real operation.",
        mode_desc,
    )


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # --usernames-file merges into the same override list as positional
    # usernames (resolve_usernames treats any non-empty override list the
    # same way regardless of source) -- lets a large ad-hoc population
    # (e.g. sample/streamers.txt) be measured without ever touching
    # the dashboard's streamers table.
    usernames_override = list(args.usernames)
    if args.usernames_file:
        for name in _read_usernames_file(args.usernames_file):
            if name not in usernames_override:
                usernames_override.append(name)

    mode_desc = (
        f"{len(usernames_override)} username(s) given on the command line/file"
        if usernames_override
        else "no usernames given -- DB-driven real-ops mode"
    )
    require_proxy_or_exit(os.environ.get("TTS_PROXY_URL"), args.allow_no_proxy, mode_desc)

    db_path = args.db_path or os.environ.get("TTS_DB_PATH", DEFAULT_DB_PATH)
    conn = db.connect(db_path)
    db.init_schema(conn)
    stale_ids = db.recover_stale_live_sessions(conn)
    if stale_ids:
        logger.warning("recovered %d session(s) left 'live' by a previous run: %s", len(stale_ids), stale_ids)

    mode_label = "measure-only, no recording" if args.measure_only else "recording enabled"
    if usernames_override:
        logger.info(
            "watching %s (explicit override, %s, poll interval: %.0fs). Ctrl+C to stop.",
            usernames_override,
            mode_label,
            args.poll_interval,
        )
    else:
        logger.info(
            "watching all non-archived streamers from the DB (re-read every cycle, %s, poll interval: %.0fs). "
            "Ctrl+C to stop.",
            mode_label,
            args.poll_interval,
        )

    # Built outside asyncio.run() on purpose, mirroring main.py: on Windows a
    # KeyboardInterrupt can be delivered while the loop is parked in a
    # blocking selector call and never reach an await point inside
    # watch_loop/run_with_reconnect, so this dict is the only place
    # guaranteed to still hold a reference to whatever's actively recording.
    # Unused in --measure-only mode (watch_loop never runs, so nothing ever
    # sets it) -- the shutdown finally block below still checks it safely.
    active: dict = {"runner": None}
    settings_kwargs = {"db_path": db_path, "idle_timeout": args.idle_timeout}

    # Resolved once at startup (not per-poll-cycle) -- fails fast on a
    # typo'd TTS_PROXY_URL before the watcher ever starts polling, same
    # reasoning as Settings.from_args's eager validation. Applies in
    # --measure-only mode too -- require_proxy_or_exit above already ran
    # unconditionally, and this is the SAME web_proxy object handed to
    # concurrency_poll_loop below, so its checks never fall back to the
    # real IP even without a recording ever starting.
    proxy_config = proxy_module.load_proxy_config(os.environ.get("TTS_PROXY_URL"))
    web_proxy = proxy_module.build_httpx_proxy(proxy_config)

    # One shared pacer for both loops below -- see CheckPacer and the
    # module docstring for why the combined rate (not each loop's rate
    # independently) is what must stay capped. web_proxy is the same
    # object passed to both, so require_proxy_or_exit's guarantee above
    # covers concurrency_poll_loop's checks too, not just watch_loop's.
    pacer = CheckPacer()

    try:
        if args.measure_only:
            asyncio.run(
                concurrency_poll_loop(
                    conn, usernames_override, pacer, args.poll_interval, args.concurrency_log_path, web_proxy
                )
            )
        else:
            asyncio.run(
                _run_watch_and_concurrency_poll(
                    watch_loop(
                        conn, usernames_override, settings_kwargs, args.poll_interval, active, pacer, web_proxy
                    ),
                    concurrency_poll_loop(
                        conn, usernames_override, pacer, args.poll_interval, args.concurrency_log_path, web_proxy
                    ),
                )
            )
    except KeyboardInterrupt:
        logger.info("keyboard interrupt received, shutting down watcher")
    finally:
        runner = active["runner"]
        if runner is not None and not runner.ended:
            runner.manual_end()
        conn.close()


if __name__ == "__main__":
    main()
