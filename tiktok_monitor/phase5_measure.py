"""Phase 5 measurement orchestrator (docs/phase5-1ip-measurement-spec.md).

Answers, for one proxy IP: how many TikTok LIVE streams can it record at
once, and for how long, before something breaks? Runs SessionRunner +
run_with_reconnect (tiktok_monitor/client.py) concurrently -- one per
currently-live streamer pulled from a pool -- exactly as they already work
for a single stream; this module only adds the orchestration layer on top
(pool-fill/replace, step schedule, anomaly classification, metrics/anomaly
logging). Nothing about comment/gift/treasure-box recording itself changes.

Two phases, switched by restarting with different CLI flags (metrics/
anomaly files are appended to, not overwritten, so history survives the
restart):
  - Phase A (space axis): starts at a low target concurrency and climbs
    (3->5->10->15->20->...) after each level holds STABILITY_HOURS with zero
    anomalies; any anomaly drops one step back down. Explores where the
    ceiling is.
  - Phase B (time axis): target concurrency fixed at whatever number you
    pass in --target-count; anomalies are recorded but never change the
    target. Explores how long that number holds up.

Anomaly taxonomy (see _record_anomaly / _resolve_pending_disconnects):
  - "disconnected": WebSocket dropped while check_is_live still says the
    streamer is live (if the streamer had actually ended, that's normal
    lifecycle, not an anomaly -- resolved via a follow-up check_is_live
    call, not by guessing from log order).
  - "connection_error": run_with_reconnect's generic exception branch.
  - "data_stall": connected, but no viewer_count event in
    STALL_THRESHOLD_SEC -- viewer_count is used (not "any event") because
    it arrives on a steady cadence on a healthy connection, so its absence
    is a much cleaner "something's wrong" signal than comment/gift going
    quiet, which happens on plenty of perfectly healthy streams.
  - "signature_rate_limit": Euler Stream's signing quota, deliberately kept
    separate from the above so it never gets mistaken for a TikTok block
    (see proxy.py's module docstring for why signing isn't proxied at all).
  - "gave_up_repeated_failures": run_with_reconnect confirmed (via a fresh
    is_live re-check) MAX_CONSECUTIVE_FAILURES times in a row that a
    streamer is still live but its WS connect keeps failing anyway, and
    stopped retrying. See the "kana18724 incident" below for why this
    exists; the streamer is quarantined for PROBLEM_STREAMER_COOLDOWN_SEC
    (see _reap_finished_slots) instead of the normal short COOLDOWN_SEC.

NOT an anomaly type / NOT counted toward Phase A's step-down, recorded
separately in data/problematic_streamers.jsonl instead:
  - "excluded_reconnect_limit": run_with_reconnect hit MAX_RECONNECTS_PER_LIVE
    signature-costing reconnects within one live (a streamer that connects
    fine, drops, reconnects, drops again -- repeating all night -- never
    trips the consecutive-failure cap above, since each cycle "succeeds"
    before failing again; see the 2026-08-27 postmortem, round two, in
    client.py's run_with_reconnect docstring). A plain offline_retry never
    counts toward this (client.start() checks is_live before signing, so
    it never spends one -- a 2026-08-28 production run wrongly quarantined
    streamers that had simply ended their broadcast before this was fixed;
    see run_with_reconnect's docstring for the full corrected accounting).
    Deliberately kept out of anomaly_counts/_step_down: this is meant to
    isolate one streamer's flakiness from Phase A's "is target_count
    itself stable" question, not
    conflate the two. Also quarantined for PROBLEM_STREAMER_COOLDOWN_SEC,
    same as gave_up_repeated_failures, but per-live rather than permanent --
    a streamer excluded today is eligible again once the pool re-scans it.

The "kana18724 incident": a real 2026-08-26 production run found one
streamer whose is_live check kept returning True while every WebSocket
connect attempt failed with InvalidStatusCode(400) -- a different TikTok
endpoint from the one is_live checks, so the two can disagree. Each
connect attempt re-fetches a fresh Euler Stream signature regardless of
whether the WS handshake that follows succeeds (client.py's
run_with_reconnect docstring has the full mechanism), and the retry loop
had no cap, so ~84 minutes of retries against this one streamer alone
exhausted the account's signing quota (minute -> hour -> day), taking
every other concurrently-monitored streamer down with it. Fixed by (1)
correcting an unrelated backoff bug where delay was reset on every
client.start() call regardless of whether it actually connected, and (2)
capping consecutive CONFIRMED-still-live connection failures per streamer
(MAX_CONSECUTIVE_FAILURES), quarantining that streamer afterward instead
of retrying forever. Round two, 2026-08-27: a full night's run exhausted
the same daily signature quota again, this time via streamers that kept
"succeeding" just long enough to reset that cap before failing again --
MAX_RECONNECTS_PER_LIVE (above) closes that gap.

Data safety: every metrics row, anomaly record, problematic-streamer
exclusion, gift-parse-failure payload, and session data-volume record is
appended AND fsync'd immediately (data/phase5_metrics.csv,
data/phase5_anomalies.jsonl, data/problematic_streamers.jsonl,
data/gift_parse_failures.jsonl, data/session_data_volume.jsonl by
default) -- a hard kill loses at most the in-flight row, never the run.
Recorded events themselves land in a separate DB file (--db-path,
independent of the production data/tts_live_tool.db) so this data is
trivially distinguishable and disposable: delete the file when done.

Zero-burst network pacing: every check_is_live call and every new
recording connection this process makes toward TikTok is serialized to one
at a time, spaced by --pool-check-interval-sec / --connection-start-
interval-sec (see the constants' comment above). This isn't just caution --
a live test during development found TikTok returning 403 for a burst of
10 concurrent check_is_live calls that an isolated single request didn't
trigger. Pool size never changes this: a 500-person pool is scanned one
username at a time, round-robin, and scanning stops entirely once
target_count is reached.
"""
import argparse
import asyncio
import csv
import dataclasses
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
import psutil

from . import db
from . import proxy as proxy_module
from .client import SessionRunner, run_with_reconnect
from .config import Settings
from .watch import check_is_live

logger = logging.getLogger(__name__)

DEFAULT_STEP_SCHEDULE = [3, 5, 10, 15, 20]
STEP_EXTENSION_INCREMENT = 5  # once past the last predefined step, keep climbing by this much
COOLDOWN_SEC = 120.0  # after a slot frees up, don't immediately re-pick the same username -- avoids thrashing on a streamer who just briefly blipped offline
DISCONNECT_RESOLVE_DELAY_SEC = 15.0  # grace period before deciding whether a "disconnected" was a real anomaly or the stream just ending

# Signature-exhaustion guard (see the "kana18724 incident" postmortem): a
# streamer whose is_live check keeps saying True while every WS connect
# attempt fails (e.g. InvalidStatusCode(400)) drains the account's Euler
# Stream signing quota on every single retry -- client.run_with_reconnect
# gives up on such a streamer after this many CONFIRMED-still-live
# consecutive failures (confirmed via a fresh is_live re-check each time,
# not just a raw failure count -- see client.py's run_with_reconnect
# docstring). MAX_CONSECUTIVE_FAILURES=None would mean unlimited, matching
# main.py/watch.py's default; Phase 5 always sets a finite cap.
MAX_CONSECUTIVE_FAILURES = 5
# The normal COOLDOWN_SEC (2 minutes) is meant for a streamer who briefly
# blipped offline -- far too short for one that just burned through
# MAX_CONSECUTIVE_FAILURES real connection attempts while still reporting
# live. Quarantine those for much longer before letting the pool scan pick
# them up again.
PROBLEM_STREAMER_COOLDOWN_SEC = 3600.0

# Second, independent signature-exhaustion guard (2026-08-27 postmortem,
# round two): a streamer that connects fine, disconnects moments later,
# reconnects fine, disconnects again -- repeating all night -- never trips
# MAX_CONSECUTIVE_FAILURES (each cycle "succeeds" before failing again),
# yet spends one signature per reconnect the whole time. This counts every
# SIGNATURE-COSTING reconnect within one live and never resets -- a plain
# offline_retry (is_live checked before signing) is free and excluded, so
# a streamer that simply ended their broadcast is never wrongly caught by
# this (2026-08-28 production fix) -- see client.py's run_with_reconnect
# docstring for the full accounting. Reuses
# PROBLEM_STREAMER_COOLDOWN_SEC's quarantine mechanism (_problem_streamers)
# once tripped, but is recorded separately (data/problematic_streamers.jsonl)
# and deliberately does NOT count as a Phase A anomaly / step-down trigger
# -- the whole point is to isolate streamer-specific noise from the
# concurrency-level question Phase A is trying to answer.
MAX_RECONNECTS_PER_LIVE = 5

# Zero-burst pacing (confirmed empirically necessary, not just cautious:
# firing 10 concurrent check_is_live calls via asyncio.gather drew a 403
# from TikTok that an isolated single request did not). Every network call
# this process makes toward TikTok -- both check_is_live polling AND
# starting a new recording connection -- is serialized to exactly one at a
# time, paced by these intervals, regardless of pool size or how many slots
# are short of target. See Orchestrator._paced_check_is_live and
# _connection_start_loop.
POOL_CHECK_INTERVAL_SEC = 5.0  # seconds between successive check_is_live calls (any reason: pool scan or disconnect resolution)
CONNECTION_START_INTERVAL_SEC = 10.0  # seconds between starting one recording connection and the next
POOL_RECHECK_COOLDOWN_SEC = 300.0  # defensive backstop only -- round-robin scanning at POOL_CHECK_INTERVAL_SEC already naturally spaces out repeat checks far more than this on any pool of a realistic size
METRICS_HEADER = [
    "timestamp",
    "phase",
    "target_count",
    "active_count",
    "cpu_percent",
    "memory_mb",
    "sign_quota_remaining",
    "cumulative_anomalies_total",
    "cumulative_disconnected",
    "cumulative_connection_error",
    "cumulative_data_stall",
    "cumulative_signature_rate_limit",
    "cumulative_gave_up_repeated_failures",
]


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


def _append_csv_row(path: str, row: list) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    is_new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(METRICS_HEADER)
        writer.writerow(row)
        f.flush()
        os.fsync(f.fileno())


def load_pool(path: str) -> list[str]:
    """One username per line; blank lines and #-comments ignored;
    duplicates collapsed (first occurrence wins, preserving order)."""
    if not os.path.exists(path):
        return []
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


class StepSchedule:
    """Phase A's target-concurrency ladder. A plain class (not just a list)
    because climbing past the predefined steps should keep going by
    STEP_EXTENSION_INCREMENT rather than dead-ending -- the whole point of
    Phase A is finding wherever the real ceiling is, even if that's higher
    than anyone guessed when writing this list."""

    def __init__(self, steps: list[int] | None = None):
        self.steps = list(steps) if steps else list(DEFAULT_STEP_SCHEDULE)

    def next_after(self, current: int) -> int:
        for step in self.steps:
            if step > current:
                return step
        return current + STEP_EXTENSION_INCREMENT

    def prev_before(self, current: int) -> int:
        """Floors at the lowest step -- if even the lowest concurrency level
        isn't stable, that itself is the finding; there's nowhere lower to
        retreat to that would still be a meaningful measurement."""
        lower = [s for s in self.steps if s < current]
        return max(lower) if lower else self.steps[0]


@dataclasses.dataclass
class Slot:
    username: str
    task: asyncio.Task
    runner: SessionRunner
    connected_at: float | None = None  # time.monotonic() of the most recent "connected" status
    stalled: bool = False  # already flagged as data-stalled; avoids re-flagging every tick while it persists
    last_session_id: int | None = None  # captured on "connected" -- runner.live_session_id is reset to None the moment the session ends, before _reap_finished_slots gets a chance to read it


class Orchestrator:
    """One instance per measurement run. Owns the pool, the currently-active
    slots (each a full SessionRunner + run_with_reconnect running as its own
    asyncio task, completely independent of the others -- one slot's
    disconnect/reconnect/error never touches another), and the metrics/
    anomaly output files."""

    def __init__(
        self,
        conn,
        base_settings: Settings,
        proxy: httpx.Proxy | None,
        pool: list[str],
        phase: str,
        target_count: int,
        metrics_path: str,
        anomalies_path: str,
        stability_sec: float,
        stall_threshold_sec: float,
        step_schedule: StepSchedule | None = None,
        pool_check_interval_sec: float = POOL_CHECK_INTERVAL_SEC,
        connection_start_interval_sec: float = CONNECTION_START_INTERVAL_SEC,
        pool_recheck_cooldown_sec: float = POOL_RECHECK_COOLDOWN_SEC,
        max_reconnects_per_live: int | None = MAX_RECONNECTS_PER_LIVE,
        problematic_streamers_path: str = "data/problematic_streamers.jsonl",
        session_volume_path: str = "data/session_data_volume.jsonl",
    ):
        self.conn = conn
        self.base_settings = base_settings
        self.proxy = proxy
        self.pool = pool
        self.phase = phase
        self.target_count = target_count
        self.metrics_path = metrics_path
        self.anomalies_path = anomalies_path
        self.stability_sec = stability_sec
        self.stall_threshold_sec = stall_threshold_sec
        self.step_schedule = step_schedule or StepSchedule()
        self.pool_check_interval_sec = pool_check_interval_sec
        self.connection_start_interval_sec = connection_start_interval_sec
        self.pool_recheck_cooldown_sec = pool_recheck_cooldown_sec
        self.max_reconnects_per_live = max_reconnects_per_live
        self.problematic_streamers_path = problematic_streamers_path
        self.session_volume_path = session_volume_path

        self.slots: dict[str, Slot] = {}
        self.cooldown_until: dict[str, float] = {}
        self.pending_disconnect_since: dict[str, float] = {}
        self.last_checked_at: dict[str, float] = {}
        self.sign_quota_state: dict = {}
        self.anomaly_counts: dict[str, int] = {
            "disconnected": 0,
            "connection_error": 0,
            "data_stall": 0,
            "signature_rate_limit": 0,
            "gave_up_repeated_failures": 0,
        }
        self.stability_start = time.monotonic()
        self.max_active_ever = 0
        self.started_at = time.monotonic()

        # Usernames run_with_reconnect just gave up on after
        # MAX_CONSECUTIVE_FAILURES confirmed-still-live connection failures
        # (see client.py's run_with_reconnect docstring and the
        # "kana18724 incident" postmortem) -- consumed by
        # _reap_finished_slots to apply PROBLEM_STREAMER_COOLDOWN_SEC
        # instead of the normal short COOLDOWN_SEC.
        self._problem_streamers: set[str] = set()

        # Zero-burst pacing state (see module docstring). _pool_cursor drives
        # round-robin scanning; _live_queue/_pending_starts decouple "found
        # live" from "actually connect" so connection starts get their own
        # independent pace, never bursting even if several checks happen to
        # find live streamers in quick succession.
        self._pool_cursor = 0
        self._live_queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending_starts: set[str] = set()
        self._check_pace_lock = asyncio.Lock()
        self._last_check_at = 0.0
        self._background_tasks: list[asyncio.Task] = []

    # --- slot lifecycle ----------------------------------------------

    def _make_on_status(self, username: str):
        def on_status(kind: str, info: dict) -> None:
            slot = self.slots.get(username)
            if kind == "connected":
                if slot is not None:
                    slot.connected_at = time.monotonic()
                    slot.last_session_id = slot.runner.live_session_id
                # A disconnect immediately followed by a reconnect is
                # resolved here rather than left to the stall/disconnect
                # checker: the reconnect itself is proof the process is
                # still trying, so there's no ambiguity left to wait out.
                self.pending_disconnect_since.pop(username, None)
            elif kind == "disconnected":
                self.pending_disconnect_since.setdefault(username, time.monotonic())
            elif kind == "connection_error":
                self._record_anomaly("connection_error", username, info)
            elif kind == "signature_rate_limit":
                self._record_anomaly("signature_rate_limit", username, info)
            elif kind == "gave_up_repeated_failures":
                self._record_anomaly("gave_up_repeated_failures", username, info)
                self._problem_streamers.add(username)
            elif kind == "excluded_reconnect_limit":
                # Deliberately NOT _record_anomaly: this is a per-streamer
                # flakiness signal, not evidence that target_count itself is
                # unstable -- see MAX_RECONNECTS_PER_LIVE's comment. Recorded
                # separately so it never touches Phase A's step-down.
                self._record_problematic_streamer(username, info)
                self._problem_streamers.add(username)
            # "offline_retry" / "user_offline_exit" / "user_not_found":
            # bookkeeping only (they free the slot naturally when
            # run_with_reconnect's task finishes) -- not anomalies on their
            # own, per the spec's explicit "natural stream end isn't an
            # anomaly" rule.

        return on_status

    def _start_slot(self, username: str) -> None:
        settings = dataclasses.replace(self.base_settings, username=username)
        runner = SessionRunner(self.conn, settings)
        on_status = self._make_on_status(username)
        task = asyncio.create_task(
            run_with_reconnect(
                runner,
                settings,
                on_status=on_status,
                sign_quota_state=self.sign_quota_state,
                check_is_live_fn=self._paced_check_is_live,
                max_consecutive_failures=MAX_CONSECUTIVE_FAILURES,
                max_reconnects_per_live=self.max_reconnects_per_live,
            )
        )
        self.slots[username] = Slot(username=username, task=task, runner=runner)
        self.max_active_ever = max(self.max_active_ever, len(self.slots))
        logger.info("slot started: @%s (active=%d/%d)", username, len(self.slots), self.target_count)

    def _record_problematic_streamer(self, username: str, info: dict) -> None:
        """data/problematic_streamers.jsonl (fsync'd): one entry per streamer
        excluded from a live for needing too many reconnects (see
        MAX_RECONNECTS_PER_LIVE). Exclusion is per-live, not permanent (see
        _reap_finished_slots) -- this file's purpose is letting repeat
        offenders across many runs/days surface later via a simple
        Group-Object/Counter over "username", not anything computed here."""
        entry = {
            "timestamp": _now_iso(),
            "username": username,
            "reconnect_count": info.get("reconnect_count"),
            "last_error": info.get("last_error"),
            "is_live_at_failure": info.get("is_live_at_failure"),
            "room_id": info.get("room_id"),
            "target_count": self.target_count,
            "active_count": len(self.slots),
        }
        _append_jsonl(self.problematic_streamers_path, entry)
        logger.warning(
            "PROBLEM STREAMER: @%s excluded from this live after %s reconnect(s) -- quarantined %.0fs (last_error=%s)",
            username,
            entry["reconnect_count"],
            PROBLEM_STREAMER_COOLDOWN_SEC,
            entry["last_error"],
        )

    def _record_session_data_volume(self, username: str, live_session_id: int) -> None:
        """data/session_data_volume.jsonl (fsync'd): one entry per ended
        live session, so avg/min/max/distribution across sessions can
        replace the cost model's placeholder 5MB/stream estimate with a
        real one (see db.get_session_data_volume)."""
        volume = db.get_session_data_volume(self.conn, live_session_id)
        entry = {
            "timestamp": _now_iso(),
            "username": username,
            "live_session_id": live_session_id,
            **volume,
        }
        _append_jsonl(self.session_volume_path, entry)
        logger.info(
            "session data volume: @%s session=%d events=%d total=%.2fMB",
            username,
            live_session_id,
            volume["event_count"],
            volume["total_bytes"] / (1024 * 1024),
        )

    def _reap_finished_slots(self) -> None:
        for username, slot in list(self.slots.items()):
            if not slot.task.done():
                continue
            if not slot.task.cancelled():
                exc = slot.task.exception()
                if exc is not None:
                    logger.error("slot @%s's task raised unexpectedly (bug, not a measured anomaly): %r", username, exc)
            if slot.last_session_id is not None:
                self._record_session_data_volume(username, slot.last_session_id)
            del self.slots[username]
            self.pending_disconnect_since.pop(username, None)
            if username in self._problem_streamers:
                self._problem_streamers.discard(username)
                self.cooldown_until[username] = time.monotonic() + PROBLEM_STREAMER_COOLDOWN_SEC
                logger.warning(
                    "slot ended: @%s quarantined for %.0fs (gave up after repeated confirmed-live connection failures)",
                    username,
                    PROBLEM_STREAMER_COOLDOWN_SEC,
                )
            else:
                self.cooldown_until[username] = time.monotonic() + COOLDOWN_SEC
            logger.info("slot ended: @%s (active=%d/%d)", username, len(self.slots), self.target_count)

    async def _paced_check_is_live(self, username: str) -> bool:
        """Every check_is_live call from this orchestrator -- pool scanning
        AND disconnect resolution alike -- goes through here. A lock plus a
        shared "last check" timestamp serializes them globally to one at a
        time, at least pool_check_interval_sec apart, no matter which
        subsystem is asking or how many want to ask at once. See the
        module docstring for why this isn't optional caution."""
        async with self._check_pace_lock:
            now = time.monotonic()
            wait = self.pool_check_interval_sec - (now - self._last_check_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_check_at = time.monotonic()
            try:
                return await check_is_live(username, web_proxy=self.proxy)
            except Exception:
                logger.debug("check_is_live raised for @%s, treating as not live", username, exc_info=True)
                return False

    def _next_candidate(self) -> str | None:
        """Round-robin through the pool, one username per call -- not a
        shuffle-and-scan-from-zero, so a 500-person pool naturally spaces
        out how often any given username gets checked (roughly once per
        "lap": pool_size * pool_check_interval_sec) without needing any
        extra bookkeeping beyond the cooldowns already tracked."""
        n = len(self.pool)
        if n == 0:
            return None
        now = time.monotonic()
        for _ in range(n):
            username = self.pool[self._pool_cursor]
            self._pool_cursor = (self._pool_cursor + 1) % n
            if (
                username not in self.slots
                and username not in self._pending_starts
                and self.cooldown_until.get(username, 0.0) <= now
                and self.last_checked_at.get(username, 0.0) + self.pool_recheck_cooldown_sec <= now
            ):
                return username
        return None  # every pool member is active, queued, or on cooldown right now

    async def _pool_check_tick(self) -> None:
        """One check, at most -- no sleeping here, so this is directly
        testable and the pacing lives entirely in _paced_check_is_live and
        the background loop's own sleep."""
        if len(self.slots) >= self.target_count:
            return  # at/above target -- the spec's explicit "stop checking once full"
        username = self._next_candidate()
        if username is None:
            return
        self.last_checked_at[username] = time.monotonic()
        is_live = await self._paced_check_is_live(username)
        logger.debug("pool check: @%s -> live=%s", username, is_live)  # --verbose to confirm one-at-a-time/interval pacing live
        if is_live:
            self._pending_starts.add(username)
            await self._live_queue.put(username)

    async def _pool_check_loop(self) -> None:
        while True:
            await self._pool_check_tick()
            await asyncio.sleep(self.pool_check_interval_sec)

    async def _connection_start_tick(self) -> bool:
        """Starts at most one queued (confirmed-live) candidate. Returns
        whether one was actually started, so the loop knows whether to take
        its full pacing pause or just poll again shortly."""
        try:
            username = self._live_queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        self._pending_starts.discard(username)
        if username in self.slots or len(self.slots) >= self.target_count:
            return False  # stale by the time we got to it (e.g. target already filled another way)
        self._start_slot(username)
        return True

    async def _connection_start_loop(self) -> None:
        while True:
            started = await self._connection_start_tick()
            await asyncio.sleep(self.connection_start_interval_sec if started else 1.0)

    def start_background_loops(self) -> None:
        """Pool-scanning and connection-starting run as their own eternal,
        independently-paced tasks -- decoupled from tick()'s poll_interval
        (reaping/stall-check/metrics) entirely, so slowing down the network-
        facing pacing never has to fight for time against those."""
        self._background_tasks = [
            asyncio.create_task(self._pool_check_loop()),
            asyncio.create_task(self._connection_start_loop()),
        ]

    # --- anomaly detection ---------------------------------------------

    async def _resolve_pending_disconnects(self) -> None:
        """Sequential and routed through _paced_check_is_live -- if several
        disconnects need resolving in the same tick, they're paced exactly
        like pool-scan checks, not fired back to back."""
        now = time.monotonic()
        for username, since in list(self.pending_disconnect_since.items()):
            if now - since < DISCONNECT_RESOLVE_DELAY_SEC:
                continue
            del self.pending_disconnect_since[username]
            if username not in self.slots:
                continue  # slot already ended naturally by the time we got to check -- not an anomaly
            still_live = await self._paced_check_is_live(username)
            if still_live:
                self._record_anomaly("disconnected", username, {"detail": "WebSocket dropped while stream was still live"})

    async def _check_data_stalls(self) -> None:
        for username, slot in self.slots.items():
            if slot.runner.live_session_id is None:
                continue
            row = self.conn.execute(
                "SELECT MAX(occurred_at) FROM live_events WHERE live_session_id = ? AND event_type = 'viewer_count'",
                (slot.runner.live_session_id,),
            ).fetchone()
            last_at = row[0] if row else None
            if last_at is None:
                continue  # no viewer_count seen yet this session -- too early to judge
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds()
            if elapsed >= self.stall_threshold_sec:
                if not slot.stalled:
                    slot.stalled = True
                    self._record_anomaly(
                        "data_stall", username, {"seconds_since_last_viewer_count": round(elapsed, 1)}
                    )
            else:
                slot.stalled = False

    def _record_anomaly(self, anomaly_type: str, username: str, detail: dict) -> None:
        self.anomaly_counts[anomaly_type] = self.anomaly_counts.get(anomaly_type, 0) + 1
        slot = self.slots.get(username)
        connection_duration = (
            round(time.monotonic() - slot.connected_at, 1) if slot and slot.connected_at is not None else None
        )
        entry = {
            "timestamp": _now_iso(),
            "phase": self.phase,
            "target_count": self.target_count,
            "active_count": len(self.slots),
            "anomaly_type": anomaly_type,
            "username": username,
            "connection_duration_sec": connection_duration,
            "detail": detail,
        }
        _append_jsonl(self.anomalies_path, entry)
        logger.warning("ANOMALY [%s] @%s at target=%d active=%d: %s", anomaly_type, username, self.target_count, len(self.slots), detail)
        if self.phase == "A":
            self._step_down()

    # --- Phase A step schedule -------------------------------------------

    def _step_down(self) -> None:
        new_target = self.step_schedule.prev_before(self.target_count)
        if new_target != self.target_count:
            logger.warning("Phase A: anomaly at target=%d, stepping DOWN to %d", self.target_count, new_target)
            self.target_count = new_target
        self.stability_start = time.monotonic()

    def _maybe_step_up(self) -> None:
        if self.phase != "A":
            return
        if time.monotonic() - self.stability_start >= self.stability_sec:
            new_target = self.step_schedule.next_after(self.target_count)
            logger.info(
                "Phase A: target=%d stable for %.0fs with zero anomalies, stepping UP to %d",
                self.target_count,
                self.stability_sec,
                new_target,
            )
            self.target_count = new_target
            self.stability_start = time.monotonic()

    # --- metrics ----------------------------------------------------------

    def _write_metrics_row(self) -> None:
        cpu_percent = psutil.cpu_percent(interval=None)
        memory_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        row = [
            _now_iso(),
            self.phase,
            self.target_count,
            len(self.slots),
            f"{cpu_percent:.1f}",
            f"{memory_mb:.1f}",
            self.sign_quota_state.get("sign_quota_remaining", ""),
            sum(self.anomaly_counts.values()),
            self.anomaly_counts["disconnected"],
            self.anomaly_counts["connection_error"],
            self.anomaly_counts["data_stall"],
            self.anomaly_counts["signature_rate_limit"],
            self.anomaly_counts["gave_up_repeated_failures"],
        ]
        _append_csv_row(self.metrics_path, row)

    # --- main tick ----------------------------------------------------

    async def tick(self) -> None:
        """Reaping/stall-check/metrics only -- pool-scanning and connection-
        starting run on their own independent paced loops (see
        start_background_loops), not here."""
        self._reap_finished_slots()
        await self._resolve_pending_disconnects()
        await self._check_data_stalls()
        self._maybe_step_up()
        self._write_metrics_row()

    def shutdown(self) -> None:
        for task in self._background_tasks:
            task.cancel()
        for username, slot in self.slots.items():
            if slot.last_session_id is not None:
                self._record_session_data_volume(username, slot.last_session_id)
            slot.task.cancel()
            slot.runner.manual_end()


async def run_orchestrator(args: argparse.Namespace) -> None:
    conn = db.connect(args.db_path)
    db.init_schema(conn)
    stale_ids = db.recover_stale_live_sessions(conn)
    if stale_ids:
        logger.warning("recovered %d session(s) left 'live' by a previous run: %s", len(stale_ids), stale_ids)

    pool = load_pool(args.pool)
    if not pool:
        logger.error(
            "pool file %s is empty or missing -- add TikTok usernames (one per line, # for comments) and restart",
            args.pool,
        )
        return
    logger.info("loaded pool: %d streamer(s) from %s", len(pool), args.pool)

    proxy_url = os.environ.get("TTS_PROXY_URL")
    proxy_config = proxy_module.load_proxy_config(proxy_url)
    proxy = proxy_module.build_httpx_proxy(proxy_config)

    base_settings = Settings(
        username="__phase5_placeholder__",  # overwritten per-slot via dataclasses.replace
        db_path=args.db_path,
        idle_timeout_sec=args.idle_timeout_sec,
        proxy_url=proxy_url,
    )

    target_count = args.target_count if args.target_count is not None else DEFAULT_STEP_SCHEDULE[0]
    if args.phase == "B" and args.target_count is None:
        logger.error("--phase B requires --target-count (the fixed concurrency level to hold and observe)")
        return

    orchestrator = Orchestrator(
        conn,
        base_settings,
        proxy,
        pool,
        phase=args.phase,
        target_count=target_count,
        metrics_path=args.metrics_path,
        anomalies_path=args.anomalies_path,
        stability_sec=args.stability_hours * 3600,
        stall_threshold_sec=args.stall_threshold_sec,
        pool_check_interval_sec=args.pool_check_interval_sec,
        connection_start_interval_sec=args.connection_start_interval_sec,
        pool_recheck_cooldown_sec=args.pool_recheck_cooldown_sec,
        max_reconnects_per_live=args.max_reconnects_per_live if args.max_reconnects_per_live > 0 else None,
        problematic_streamers_path=args.problematic_streamers_path,
        session_volume_path=args.session_volume_path,
    )

    logger.info(
        "Phase 5 measurement starting: phase=%s target=%d pool_size=%d db=%s",
        args.phase,
        target_count,
        len(pool),
        args.db_path,
    )
    logger.info(
        "zero-burst pacing: 1 check_is_live call every %.1fs (round-robin, stops entirely once at target), "
        "1 new recording connection every %.1fs",
        args.pool_check_interval_sec,
        args.connection_start_interval_sec,
    )

    orchestrator.start_background_loops()
    try:
        while True:
            await orchestrator.tick()
            await asyncio.sleep(args.poll_interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("stopping measurement run (keyboard interrupt)")
    finally:
        orchestrator.shutdown()
        conn.close()
        _emit_summary(args.metrics_path, args.anomalies_path, args.summary_path, args.session_volume_path)


# --- summary report (spec section F) -----------------------------------


def _read_metrics_rows(metrics_path: str) -> list[dict]:
    if not os.path.exists(metrics_path):
        return []
    with open(metrics_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_anomalies(anomalies_path: str) -> list[dict]:
    if not os.path.exists(anomalies_path):
        return []
    entries = []
    with open(anomalies_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def generate_summary(metrics_path: str, anomalies_path: str, session_volume_path: str | None = None) -> str:
    """Produces the spec's section-F report from whatever metrics/anomaly
    data exists on disk -- safe to call mid-run (nothing here requires the
    orchestrator to have exited cleanly, which is the whole point: data
    already flushed to these files survives a crash)."""
    rows = _read_metrics_rows(metrics_path)
    anomalies = _read_anomalies(anomalies_path)

    lines = ["=== Phase 5 実測サマリー ===", ""]

    if not rows:
        lines.append("メトリクスデータがまだありません。")
        return "\n".join(lines)

    max_active = max(int(r["active_count"]) for r in rows)
    total_anomalies = len(anomalies)
    first_ts = datetime.fromisoformat(rows[0]["timestamp"])
    last_ts = datetime.fromisoformat(rows[-1]["timestamp"])
    total_runtime_hours = (last_ts - first_ts).total_seconds() / 3600

    lines.append(f"総運転時間: 約{total_runtime_hours:.1f}時間 ({rows[0]['timestamp']} 〜 {rows[-1]['timestamp']})")
    lines.append(f"到達した最大同時本数: {max_active}")
    lines.append(f"総異常回数: {total_anomalies}")
    lines.append("")

    lines.append("--- 同時本数ごとの CPU/メモリ ピーク ---")
    by_target: dict[str, list[dict]] = {}
    for r in rows:
        by_target.setdefault(r["target_count"], []).append(r)
    for target in sorted(by_target, key=lambda t: int(t)):
        group = by_target[target]
        peak_cpu = max(float(r["cpu_percent"]) for r in group)
        peak_mem = max(float(r["memory_mb"]) for r in group)
        lines.append(f"  目標{target}本: CPU peak {peak_cpu:.1f}% / メモリ peak {peak_mem:.1f}MB ({len(group)}件のサンプル)")
    lines.append("")

    lines.append("--- 異常の内訳 ---")
    by_type: dict[str, int] = {}
    for a in anomalies:
        by_type[a["anomaly_type"]] = by_type.get(a["anomaly_type"], 0) + 1
    if by_type:
        for anomaly_type, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {anomaly_type}: {count}件")
    else:
        lines.append("  異常なし")
    lines.append("")

    if anomalies:
        lines.append("--- 異常の発生タイミング(本数別) ---")
        by_target_count: dict[str, list[dict]] = {}
        for a in anomalies:
            by_target_count.setdefault(str(a["target_count"]), []).append(a)
        for target, entries in sorted(by_target_count.items(), key=lambda kv: int(kv[0])):
            types = {}
            for e in entries:
                types[e["anomaly_type"]] = types.get(e["anomaly_type"], 0) + 1
            type_str = ", ".join(f"{k}×{v}" for k, v in types.items())
            lines.append(f"  目標{target}本の時: {len(entries)}件 ({type_str})")
        lines.append("")

    sign_quota_rows = [r for r in rows if r["sign_quota_remaining"]]
    if len(sign_quota_rows) >= 2:
        first_q = int(sign_quota_rows[0]["sign_quota_remaining"])
        last_q = int(sign_quota_rows[-1]["sign_quota_remaining"])
        elapsed_hours = (
            datetime.fromisoformat(sign_quota_rows[-1]["timestamp"]) - datetime.fromisoformat(sign_quota_rows[0]["timestamp"])
        ).total_seconds() / 3600
        if elapsed_hours > 0:
            lines.append("--- 署名消費ペース ---")
            lines.append(f"  {first_q} -> {last_q} (残数の推移、{elapsed_hours:.1f}時間で参考値)")
            lines.append("")

    if session_volume_path:
        volumes = _read_jsonl(session_volume_path)
        if volumes:
            event_counts = [int(v["event_count"]) for v in volumes]
            total_bytes_list = [int(v["total_bytes"]) for v in volumes]
            lines.append("--- 1配信あたりのデータ量(実測、コスト試算用) ---")
            lines.append(f"  セッション数: {len(volumes)}")
            lines.append(
                f"  イベント数: 平均{sum(event_counts) / len(event_counts):.0f} "
                f"/ 最小{min(event_counts)} / 最大{max(event_counts)}"
            )
            avg_mb = sum(total_bytes_list) / len(total_bytes_list) / (1024 * 1024)
            min_mb = min(total_bytes_list) / (1024 * 1024)
            max_mb = max(total_bytes_list) / (1024 * 1024)
            lines.append(f"  推定データ量: 平均{avg_mb:.2f}MB / 最小{min_mb:.2f}MB / 最大{max_mb:.2f}MB")
            lines.append("")

    # Deliberately not asserting a single confident "safe up to N" number --
    # that would require knowing which levels held for the full stability
    # window vs. were only briefly visited, which this simple CSV/JSONL
    # summary doesn't attempt to model. Report what was actually observed
    # and let the human reading it (who also has the full anomaly list and
    # per-level breakdown above) draw that conclusion.
    non_signature_anomalies = [a for a in anomalies if a["anomaly_type"] != "signature_rate_limit"]
    lines.append("--- 結論(参考) ---")
    if non_signature_anomalies:
        anomalous_targets = sorted({int(a["target_count"]) for a in non_signature_anomalies})
        clean_targets = sorted(int(t) for t in by_target if int(t) not in anomalous_targets)
        if clean_targets:
            lines.append(f"異常が一度も記録されていない目標本数: {clean_targets}")
        lines.append(f"TikTok側の異常(切断/接続エラー/データ停止)が記録された目標本数: {anomalous_targets}")
        lines.append("上の「異常の発生タイミング」と合わせて、どの本数から不安定になったか確認してください。")
    else:
        lines.append(f"観測期間中、TikTok側の異常(切断/接続エラー/データ停止)は一度も記録されず、最大同時{max_active}本まで安定。")
    if any(a["anomaly_type"] == "signature_rate_limit" for a in anomalies):
        lines.append("署名上限(Euler Stream)には別途到達している区間があります -- 上の「異常の内訳」を参照。")

    return "\n".join(lines)


def _emit_summary(
    metrics_path: str, anomalies_path: str, summary_path: str, session_volume_path: str | None = None
) -> None:
    """Prints AND writes the summary to a file. The file exists specifically
    because Windows consoles frequently mangle Japanese text printed
    directly (cp932 vs. UTF-8) -- a file written with explicit UTF-8
    encoding is the reliable way to actually read this, and it also means
    the summary survives being reviewed after the terminal that ran this is
    long gone (useful mid-run during a multi-day measurement, not just at
    the end)."""
    summary = generate_summary(metrics_path, anomalies_path, session_volume_path)
    directory = os.path.dirname(summary_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)
    try:
        print(summary)
    except UnicodeEncodeError:
        print(f"(summary written to {summary_path} -- this console can't display it directly)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pool", default="tiktok_monitor/phase5_pool.txt", help="Pool file, one username per line")
    parser.add_argument("--db-path", default="data/phase5_measure.db", help="Separate DB file for measurement recordings")
    parser.add_argument("--metrics-path", default="data/phase5_metrics.csv")
    parser.add_argument("--anomalies-path", default="data/phase5_anomalies.jsonl")
    parser.add_argument(
        "--problematic-streamers-path",
        default="data/problematic_streamers.jsonl",
        help="Per-live exclusion records for streamers that needed too many reconnects (see --max-reconnects-per-live)",
    )
    parser.add_argument(
        "--session-volume-path",
        default="data/session_data_volume.jsonl",
        help="Per-session recorded data volume (event count / estimated bytes), for the cost model's per-stream estimate",
    )
    parser.add_argument("--summary-path", default="data/phase5_summary.txt", help="Where the final/mid-run summary report is written (UTF-8 -- see _emit_summary)")
    parser.add_argument("--phase", choices=["A", "B"], default="A")
    parser.add_argument(
        "--target-count",
        type=int,
        default=None,
        help="Phase A: starting target (default: first step, 3). Phase B: required, the fixed level to hold.",
    )
    parser.add_argument("--poll-interval", type=float, default=30.0, help="Seconds between orchestrator ticks")
    parser.add_argument("--stability-hours", type=float, default=1.0, help="Phase A: hours of zero anomalies before stepping up")
    parser.add_argument("--stall-threshold-sec", type=float, default=180.0, help="Seconds without a viewer_count event before flagging a data stall")
    parser.add_argument(
        "--pool-check-interval-sec",
        type=float,
        default=POOL_CHECK_INTERVAL_SEC,
        help=f"Seconds between successive check_is_live calls, one at a time, no matter the pool size (default: {POOL_CHECK_INTERVAL_SEC:.0f}). "
        "Start conservative (long) and shorten later once stability is confirmed.",
    )
    parser.add_argument(
        "--connection-start-interval-sec",
        type=float,
        default=CONNECTION_START_INTERVAL_SEC,
        help=f"Seconds between starting one new recording connection and the next, one at a time (default: {CONNECTION_START_INTERVAL_SEC:.0f})",
    )
    parser.add_argument(
        "--pool-recheck-cooldown-sec",
        type=float,
        default=POOL_RECHECK_COOLDOWN_SEC,
        help=f"Defensive backstop: minimum seconds between two check_is_live calls for the same pool username (default: {POOL_RECHECK_COOLDOWN_SEC:.0f})",
    )
    parser.add_argument(
        "--max-reconnects-per-live",
        type=int,
        default=MAX_RECONNECTS_PER_LIVE,
        help=f"Exclude a streamer from the current live after this many signature-costing reconnects "
        f"(a free offline_retry doesn't count -- see client.py's run_with_reconnect docstring) "
        f"(default: {MAX_RECONNECTS_PER_LIVE}); 0 or negative disables this guard. Independent of the "
        "consecutive-failure cap.",
    )
    parser.add_argument("--idle-timeout-sec", type=float, default=60.0)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="Print the summary report from existing --metrics-path/--anomalies-path and exit (no connections made)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.summarize:
        _emit_summary(args.metrics_path, args.anomalies_path, args.summary_path, args.session_volume_path)
        return
    asyncio.run(run_orchestrator(args))


if __name__ == "__main__":
    main()
