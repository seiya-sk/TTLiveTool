import asyncio
import base64
import json
import logging
import os
import re
import time
import httpx

from datetime import datetime, timezone
from typing import Callable

from TikTokLive import TikTokLiveClient
from TikTokLive.client.errors import SignatureRateLimitError, UserNotFoundError, UserOfflineError
from TikTokLive import events as tiktok_events
from TikTokLive.events import (
    CommentEvent,
    ConnectEvent,
    DisconnectEvent,
    EnvelopeEvent,
    FollowEvent,
    GiftEvent,
    JoinEvent,
    LikeEvent,
    RoomUserSeqEvent,
    ShareEvent,
)

from . import db
from . import events as event_normalizers
from . import fetch_avatars as avatar_module
from . import proxy as proxy_module
from . import screenshot as screenshot_module
from .config import Settings
from .idle_watchdog import IdleWatchdog

logger = logging.getLogger(__name__)

# Optional extension point (Phase 5 concurrent-measurement prep): called as
# on_status(kind, info) at points of interest (connected/disconnected/
# offline-retry/signature-rate-limit/connection-error/etc). None everywhere
# by default -- main.py/watch.py never pass one, so single-streamer
# recording behaves exactly as before this existed. Exists specifically so
# a multi-streamer orchestrator running several SessionRunners concurrently
# can attribute each status change to the right streamer without parsing
# shared log output (several of the log messages below don't include the
# username, since they were never meant to be told apart from each other).
StatusCallback = Callable[[str, dict], None]

# hard_stop() が disconnect() を待つ上限。ハードタイムアウトは「相手が
# 黙っている」状態を畳む処理なので、その後始末で無期限に待ってはいけない。
HARD_STOP_DISCONNECT_TIMEOUT_SEC = 10.0


# WebSocket の正常終了コード。websockets の ConnectionClosed 系例外は
# .rcvd / .code で閉鎖コードを持つが、バージョンによって形が違うので
# repr からも拾えるようにしておく(ops/error_notifier.py と同じ方針)。
_NORMAL_CLOSURE_CODE = 1000
_CLOSE_CODE_RE = re.compile(r"CloseCode\.\w+:\s*(\d+)|code=(\d+)")


def _is_normal_closure(exc: Exception) -> bool:
    if "ConnectionClosed" not in type(exc).__name__ and "ConnectionClosed" not in repr(exc):
        return False
    code = getattr(exc, "code", None)
    if code is None:
        rcvd = getattr(exc, "rcvd", None)
        code = getattr(rcvd, "code", None)
    if code is None:
        m = _CLOSE_CODE_RE.search(repr(exc))
        if m:
            code = int(m.group(1) or m.group(2))
    return code == _NORMAL_CLOSURE_CODE


# --- 署名リクエストをプロキシ経由にする ------------------------------------
# Euler Stream(api.eulerstream.com)のレート制限は **IP単位** であることを
# 2026-09-02 に実測で確認した。/webcast/rate_limits を実IPとプロキシ経由の
# 両方から叩いた結果:
#     VPS実IP        day 0/100   ← 枯渇
#     10本のプロキシ  day 100/100 ← すべて未使用
# つまり署名を各プロキシIPから出せば、日次予算は 100 → 1,000 に増える。
#
# 制限値(2026-09-02 実測): day 100 / hour 30 / minute 5(IPごと)。
# 1日100件は、150人プールを12.5分周期で巡回する運用だと 19時台で使い切った。
#
# proxy.py の docstring には「TikTokSigner はプロキシ引数を持たないので
# 署名は必ず実IPから出る」と書かれているが、これは TikTokLive 7.0.0 では
# 正確ではない。TikTokSigner.__init__ 自体にプロキシ引数は無いものの、
# 内部の EulerApiSdk Client は set_async_httpx_client() で httpx クライアントを
# 差し替えられる。そこにプロキシ付きのクライアントを注入すればよい。
def _route_signer_through_proxy(client, proxy_url: str | None) -> "httpx.AsyncClient | None":
    """署名クライアントを proxy_url 経由に差し替える。差し替えたクライアントを
    返す(呼び出し側が後で閉じられるように)。失敗しても録画は続けたいので、
    例外は握りつぶして None を返す -- その場合は従来どおり実IPから署名する。"""
    if not proxy_url:
        return None
    try:
        sdk = client.web.signer.sdk_client
        headers = dict(getattr(sdk, "_headers", None) or {})
        # APIキーを設定している場合、EulerApiSdk の AuthenticatedClient は
        # 認証ヘッダを _headers ではなく get_async_httpx_client() の中で
        # 遅延注入する。こちらが先に set_async_httpx_client() で差し替えると
        # そのコードが二度と走らず、**APIキーが送られないまま**になる
        # (無料の匿名扱いに戻る)。ここで同じヘッダを自分で組み立てておく。
        token = getattr(sdk, "token", None)
        if token:
            name = getattr(sdk, "auth_header_name", "Authorization")
            prefix = getattr(sdk, "prefix", "")
            headers[name] = f"{prefix} {token}" if prefix else token
        proxied = httpx.AsyncClient(
            base_url=getattr(sdk, "_base_url", None),
            cookies=getattr(sdk, "_cookies", None) or {},
            headers=headers,
            timeout=getattr(sdk, "_timeout", None),
            verify=getattr(sdk, "_verify_ssl", True),
            follow_redirects=getattr(sdk, "_follow_redirects", False),
            proxy=proxy_url,
            **(getattr(sdk, "_httpx_args", None) or {}),
        )
        sdk.set_async_httpx_client(proxied)
        return proxied
    except Exception as exc:
        # ライブラリ側の内部構造が変わった場合はここに落ちる。署名が実IPから
        # 出るだけで録画自体は動くので、警告にとどめる。
        logger.warning("could not route signing through the proxy (falling back to the real IP): %s", exc)
        return None


def _notify(on_status: StatusCallback | None, kind: str, **info) -> None:
    if on_status is None:
        return
    try:
        on_status(kind, info)
    except Exception:
        logger.debug("on_status callback raised for kind=%s, ignoring", kind, exc_info=True)


# Substring fingerprints (matched against str(exception)) for upstream
# TikTokLiveProto schema bugs confirmed harmless -- the affected message
# type carries no data this project records. Add to this list only after
# confirming a given parse failure doesn't touch comment/gift/treasure-box
# recording; TikTokLive logs anything not on the list at ERROR with a full
# traceback, which is the right default for anything unconfirmed.
_KNOWN_HARMLESS_PARSE_ERRORS = [
    "HashtagNamespace",  # WebcastLinkLayerMessage -- unused internal field, unrelated to any event this project handles
]

# UNLIKE the list above, these are NOT confirmed harmless -- they are a
# confirmed real, if rare, gift data loss (see the 2026-08-27 gift-loss
# investigation): TikTokLiveProto's SchemeInfo.scheme_gift_type field has a
# broken forward-reference type resolution (raises AttributeError:
# 'bytes' object has no attribute 'GiftStructSchemeGiftType') that
# intermittently fails to parse WebcastGiftMessage payloads -- observed on
# ~0.06% of gifts, seemingly correlated with larger/special gifts that
# populate scheme_info (an ordinary small gift doesn't). On failure,
# TikTokLive's _parse_webcast_response_message returns only the generic
# envelope event -- the GiftEvent for that message is never emitted, and
# this project has no other listener that could recover it (see
# build_client's parse_error_ignorelist wiring below).
#
# Demoted from ERROR+full-traceback to a single WARNING line ONLY because
# _attach_gift_parse_failure_capture (below) independently saves the full
# raw payload to GIFT_PARSE_FAILURE_LOG_PATH first -- this is a deliberate
# mitigation (preserve the bytes for later recovery), not a decision that
# the data loss doesn't matter. Do not add entries here casually; move a
# fingerprint from _KNOWN_LOSSY_PARSE_ERRORS to _KNOWN_HARMLESS_PARSE_ERRORS
# only after separately confirming it never touches recorded data.
_KNOWN_LOSSY_PARSE_ERRORS = [
    "GiftStructSchemeGiftType",  # WebcastGiftMessage -- see above; payload is captured, not just silenced
]

GIFT_PARSE_FAILURE_LOG_PATH = "data/gift_parse_failures.jsonl"

# DBに書けなかったイベントの退避先。**握りつぶさないための受け皿。**
# 2026-09-02、掃除ジョブとの書き込み競合で24件のイベントが失われた。
# 再試行(db.write_with_retry)を入れてもなお書けなかった場合、ここに
# 全内容を残して後から復元できるようにする。件数が増えるようなら
# 競合そのものを疑う指標にもなる。
FAILED_EVENT_LOG_PATH = "data/failed_events.jsonl"


def _record_failed_event(entry: dict) -> None:
    """DBに書けなかったイベントを、失わないようにファイルへ落とす。

    fsync まで行う(gift の取りこぼし退避と同じ作法)。ここまで来た時点で
    DBは書けない状態なので、DBに書き直す選択肢は無い -- ファイルが唯一の
    保存先になる。書式は live_events の列にそのまま対応させ、後から
    一括で流し込めるようにしてある。
    """
    directory = os.path.dirname(FAILED_EVENT_LOG_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        with open(FAILED_EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        # ここで失敗したら本当に打つ手が無い。せめてログには残す。
        logger.exception("failed-event の退避にも失敗した: %s", entry.get("event_type"))


def _record_gift_parse_failure(payload: bytes | None, username: str | None = None) -> None:
    """Appends the full raw protobuf bytes (base64) for one failed
    WebcastGiftMessage parse, fsync'd immediately (same durability pattern
    as phase5_measure.py's anomaly log -- a hard kill loses at most the
    in-flight entry). The bytes are very likely intact protobuf (this is a
    Python-side type-hint resolution bug in the library, not wire-format
    corruption -- see _KNOWN_LOSSY_PARSE_ERRORS), so they're worth keeping
    even though this project can't decode them today.

    username identifies which streamer's connection this happened on --
    without it, a mid-run `Get-Content -Tail` of this file can't tell you
    whose gift just got lost."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "username": username,
        "payload_bytes": len(payload) if payload else 0,
        "payload_b64": base64.b64encode(payload).decode("ascii") if payload else None,
    }
    directory = os.path.dirname(GIFT_PARSE_FAILURE_LOG_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(GIFT_PARSE_FAILURE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    logger.warning(
        "gift parse failure: raw payload captured (%d bytes) for @%s -- see %s",
        entry["payload_bytes"],
        username,
        GIFT_PARSE_FAILURE_LOG_PATH,
    )


def _attach_gift_parse_failure_capture(client: TikTokLiveClient, username: str | None = None) -> None:
    """Wraps this client instance's message parser so a WebcastGiftMessage
    parse failure captures the raw payload instead of just being silently
    demoted (see _KNOWN_LOSSY_PARSE_ERRORS). Detects failure the same way
    TikTokLive's own code distinguishes success from failure: on success,
    _parse_webcast_response_message always returns at least
    [response_event, proto_event, ...]; on a caught parse exception it
    returns only [response_event]. Per-instance (not a class-level patch),
    so it only ever touches this one connection and never affects the
    normal parse/return path for every other message, gift or otherwise."""
    original = client._parse_webcast_response_message

    async def _wrapped(webcast_response, webcast_response_message):
        events = await original(
            webcast_response=webcast_response, webcast_response_message=webcast_response_message
        )
        if (
            webcast_response_message is not None
            and webcast_response_message.method == "WebcastGiftMessage"
            and len(events) <= 1
        ):
            _record_gift_parse_failure(webcast_response_message.payload, username=username)
        return events

    client._parse_webcast_response_message = _wrapped


_EVENT_NORMALIZERS = {
    CommentEvent: event_normalizers.normalize_comment,
    GiftEvent: event_normalizers.normalize_gift,
    RoomUserSeqEvent: event_normalizers.normalize_viewer_count,
    LikeEvent: event_normalizers.normalize_like,
    FollowEvent: event_normalizers.normalize_follow,
    ShareEvent: event_normalizers.normalize_share,
    JoinEvent: event_normalizers.normalize_room_enter,
}

# PK battles fire under whichever Link/Battle/Armies event class the
# protocol happens to use for a given TikTok LIVE (this has shifted across
# protocol/library versions before). Discovered dynamically instead of
# hardcoded so a future TikTokLive release that renames/adds one still gets
# picked up automatically.
# 観測専用: ライブの明示的な終了/一時停止を示す可能性のあるイベント。
# TikTokLive のバージョンによって有無が変わるので、getattr で存在するものだけ
# を拾う(無いクラスを直接 import すると起動しなくなる)。
_OBSERVED_CONTROL_EVENT_NAMES = (
    "LiveEndEvent",
    "LivePauseEvent",
    "LiveUnpauseEvent",
    "ControlEvent",
)


def _discover_observed_control_event_classes() -> list[type]:
    classes = []
    for name in _OBSERVED_CONTROL_EVENT_NAMES:
        cls = getattr(tiktok_events, name, None)
        if cls is not None:
            classes.append(cls)
    return classes


_OBSERVED_CONTROL_EVENT_CLASSES = _discover_observed_control_event_classes()

# 終了/一時停止の判定に使うクラス。存在しないライブラリ版でも起動できるよう
# getattr で解決し、None ならそのリスナーを張らない(= 従来どおりの挙動)。
_LIVE_END_EVENT_CLS = getattr(tiktok_events, "LiveEndEvent", None)
_LIVE_PAUSE_EVENT_CLS = getattr(tiktok_events, "LivePauseEvent", None)
_LIVE_UNPAUSE_EVENT_CLS = getattr(tiktok_events, "LiveUnpauseEvent", None)


_BATTLE_EVENT_KEYWORDS = ("Link", "Battle", "Armies")


def _discover_battle_event_classes() -> list[type]:
    classes = []
    for name in dir(tiktok_events):
        if not name.endswith("Event"):
            continue
        if not any(keyword in name for keyword in _BATTLE_EVENT_KEYWORDS):
            continue
        obj = getattr(tiktok_events, name)
        if isinstance(obj, type):
            classes.append(obj)
    return classes


_BATTLE_EVENT_CLASSES = _discover_battle_event_classes()


def _attach_sign_rate_limit_logger(client: TikTokLiveClient, quota_state: dict | None = None) -> None:
    """Best-effort: logs Euler Stream's RateLimit-Remaining header on every
    sign-server response, not just when the quota is actually exhausted --
    lets Phase 5 measurement watch the signing quota deplete in real time
    instead of finding out only once it's gone. Reaches through
    TikTokLive's public client.web.signer.sdk_client accessor chain (public
    properties, not private attributes), but is still wrapped defensively:
    if a future TikTokLive release restructures this, the hook just never
    gets attached and this feature silently stops logging rather than
    breaking connection setup.

    quota_state is optional (Phase 5 concurrent-measurement prep): when
    given, the latest remaining count is also written to
    quota_state["sign_quota_remaining"] so an orchestrator polling many
    concurrent connections can read it without scraping logs. The quota is
    one account-level Euler Stream budget shared by every connection this
    process makes, so any one connection's hook firing is a globally valid
    reading -- last-write-wins across concurrent slots is fine."""
    try:
        httpx_client = client.web.signer.sdk_client.get_async_httpx_client()
    except AttributeError as exc:
        logger.debug("could not attach sign-server quota logger (library structure changed?): %r", exc)
        return

    async def _on_sign_server_response(response) -> None:
        remaining = response.headers.get("RateLimit-Remaining")
        if remaining is not None:
            logger.info("Euler Stream sign server quota remaining: %s", remaining)
            if quota_state is not None:
                try:
                    quota_state["sign_quota_remaining"] = int(remaining)
                except (TypeError, ValueError):
                    pass

    httpx_client.event_hooks.setdefault("response", []).append(_on_sign_server_response)


class SessionRunner:
    """Owns DB state for one monitoring run and wires TikTokLive events into
    it. A single instance spans reconnects; each reconnect that happens
    before the idle watchdog fires reuses the same live_session row."""

    def __init__(self, conn, settings: Settings):
        self.conn = conn
        self.settings = settings
        self.streamer_id = db.get_or_create_streamer(conn, settings.username)
        self.live_session_id: int | None = None
        # 乗り換え元から引き継ぐセッションID(proxy_pool_trial が設定する)。
        # 設定されていれば room_id 判定より優先する -- 呼び出し側が既に
        # 「同じライブだ」と確認済みだから。
        self.resume_session_id: int | None = None
        # 明示的な終了シグナル(LiveEndEvent / NORMAL_CLOSURE)を受け取ったか。
        # 監督ループはこれを見て「再確認せず即終了」と判断する。
        self.live_ended_signal: str | None = None
        # 最終イベント時刻(time.monotonic)。監督ループが無イベント検知に使う。
        self.last_event_at: float = time.monotonic()
        # LivePauseEvent 中は無イベントが正常なので、生存確認の対象から外す。
        self.paused: bool = False
        self.watchdog: IdleWatchdog | None = None
        # 署名用に差し替えた httpx クライアント(プロキシ経由)。再接続の
        # たびに作り直すので、古いものは閉じる。
        self._signer_http_client = None
        self.client: TikTokLiveClient | None = None
        self._ended = False
        self._recorded_opponents: set[str] = set()
        self._screenshot_task: asyncio.Task | None = None
        self._avatar_task: asyncio.Task | None = None

    def _ensure_session(self) -> None:
        # 終了確定済みのランナーは、もう新しいセッションを作らない。
        # LiveEndEvent / NORMAL_CLOSURE で end_now() した直後、切断処理の
        # 最中に「通信中だった」イベントが遅れて届く。end_now() が
        # live_session_id を None にしているため、その遅延イベントが
        # _ensure_session() を呼んで空のセッションを作ってしまっていた
        # (2026-09-01 実例: session 46 -- イベント2件、しかもどちらも
        # セッション作成時刻より前のもの。同じ room_id が2セッションに
        # 分かれて見える原因になっていた)。
        #
        # この判定を _ended だけで行うのが要点。room_id や経過時間で
        # 「幻かどうか」を推測すると、本物の配信を取りこぼす危険がある
        # (例: NORMAL_CLOSURE が一時的なサーバ都合で出て配信は続いていた
        # 場合、room_id で新規作成を止めると残り全部を失う)。
        # _ended はこの runner インスタンスの状態でしかないので、本物の
        # 再開・新配信は影響を受けない -- それらはプールが新しい録画タスクを
        # 起こし、新しい SessionRunner(_ended=False)が処理するため。
        if self._ended:
            return
        if self.live_session_id is None:
            room_id = self.current_room_id()
            # 同じ room_id のライブがまだ続いているなら、新しい行を作らずに
            # その行へ書き戻す。これが「1本の配信 = 1セッション」を保つ唯一の
            # 仕組みで、IP乗り換え(同一プロセス)と収集プロセスの再起動跨ぎの
            # 両方を、同じ判定でカバーする。room_id が取れなければ再開せず
            # 新規作成する -- 判定できないまま繋ぐと別ライブが混ざる。
            resumable = self.resume_session_id or db.find_resumable_session(
                self.conn, self.streamer_id, room_id, self.settings.resume_window_sec
            )
            if resumable is not None:
                db.resume_session(self.conn, resumable)
                db.set_session_room_id(self.conn, resumable, room_id)
                self.live_session_id = resumable
                self.resume_session_id = None
                logger.info(
                    "live_session resumed id=%s room_id=%s (@%s)",
                    self.live_session_id, room_id, self.settings.username,
                )
                self._recorded_opponents = set()
                self._start_watchdog()
                # **再開でもここを通す。** 以前は下の新規作成側にしか
                # 置いておらず、再開したセッションは1枚も撮れなかった
                # (2026-09-02 の棚卸しで判明: セッション78-90 の13本が
                # すべて0枚)。この return は「live_sessions に行を作らない」
                # ためだけのもので、スクショ/アバターを飛ばす理由は無かった。
                # _start_watchdog() より後、という順序は新規作成側と揃える。
                self._schedule_start_tasks()
                return

            self.live_session_id = db.create_live_session(
                self.conn, self.streamer_id, room_id=room_id
            )
            logger.info(
                "live_session started id=%s room_id=%s", self.live_session_id, room_id
            )
            self._recorded_opponents = set()
            self._start_watchdog()
            self._schedule_start_tasks()

    def _schedule_start_tasks(self) -> None:
        """スクリーンショットとアバター取得を仕掛ける。**新規作成と再開の両方から呼ぶ。**

        多重起動しないこと: 同じランナーで2度呼ばれても、一度仕掛けたら
        作り直さない。プロセスをまたぐ多重(再起動後のランナーが、前の
        プロセスの撮った1枚を知らずにもう1枚撮る)は、メモリ上のフラグでは
        防げないので _capture_screenshot_later 側が live_screenshots を見て
        防ぐ。二段構えなのはそのため。
        """
        # One screenshot per live (design doc section 3); fire-and-forget so
        # a slow headless-browser capture never delays comment/gift event
        # processing. Skippable via TTS_DISABLE_SCREENSHOTS (see config.py)
        # -- e.g. a host without Playwright's Chromium installed.
        if self.settings.screenshots_enabled and self._screenshot_task is None:
            self._screenshot_task = asyncio.get_event_loop().create_task(
                self._capture_screenshot_later(self.live_session_id)
            )
        # fetch_avatars' underlying TikTok endpoint only returns
        # owner/avatar data while the room is resolvable -- confirmed
        # empirically to come back empty for most streamers who aren't
        # CURRENTLY live (see fetch_avatars.py's module docstring).
        # Right here, mid-connect, the room is guaranteed resolvable, so
        # this is the one reliable place to catch every streamer's
        # avatar within their first tracked stream. The dashboard's
        # "add streamer" flow also attempts a fetch immediately (best-
        # effort, works if they streamed recently enough that TikTok
        # still serves cached room data) -- this is the guaranteed
        # fallback for everyone else.
        if self._avatar_task is None:
            self._avatar_task = asyncio.get_event_loop().create_task(self._maybe_fetch_avatar())

    def _screenshot_wait_sec(self, live_session_id: int) -> float:
        """あと何秒待てば「セッション開始から screenshot_delay_sec」になるか。

        **タスクを仕掛けた時刻ではなく、セッション開始時刻から数える。**
        再開のたびに仕掛け直すので、仕掛けた時刻を基準にすると再開のたびに
        10分待ち直すことになる。10分より短い間隔で再起動や乗り換えが起きる
        配信は、いつまでも1枚も撮れない。すでに過ぎていれば 0 を返す
        (= 即座に撮る)。
        """
        delay = self.settings.screenshot_delay_sec
        started_at = db.get_session_started_at(self.conn, live_session_id)
        if not started_at:
            return delay
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            # 想定外の書式。待たずに撮るより、従来どおり待つほうが安全
            # (開始直後は準備画面や暗転が写る)。
            return delay
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return max(0.0, delay - elapsed)

    async def _capture_screenshot_later(self, live_session_id: int) -> None:
        """セッション開始から screenshot_delay_sec 経った時点で1枚撮る。

        開始直後を避けるのは、配信の準備画面や暗転が写りやすいため。
        待っている間に配信が終われば end_now() がこのタスクを cancel する
        ので、短い配信は撮られない。
        """
        # 既に撮ってあるセッションなら、待つ意味も撮る意味もない
        # (再起動で再開したときにここへ来る)。
        if db.session_has_screenshot(self.conn, live_session_id):
            return
        try:
            remaining = self._screenshot_wait_sec(live_session_id)
            if remaining > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return
        # 待っている間に別のランナー(乗り換え先や再起動後)が撮っている
        # ことがある。live_screenshots に一意制約が無いので、直前にもう一度見る。
        if db.session_has_screenshot(self.conn, live_session_id):
            return
        await self._capture_screenshot(live_session_id)

    async def _capture_screenshot(self, live_session_id: int) -> None:
        output_path = screenshot_module.default_screenshot_path(
            self.settings.screenshot_dir, live_session_id
        )
        try:
            success = await screenshot_module.capture_live_screenshot(
                self.settings.username, output_path, proxy_url=self.settings.proxy_url
            )
        except Exception as exc:
            logger.warning("screenshot capture raised unexpectedly: %s", exc)
            return
        if success:
            db.insert_screenshot(self.conn, live_session_id, output_path)

    async def _maybe_fetch_avatar(self) -> None:
        if db.get_streamer_avatar_path(self.conn, self.streamer_id) is not None:
            return  # already cached; don't refetch on every stream start
        try:
            await avatar_module.fetch_and_store_avatar(
                self.conn, self.streamer_id, self.settings.username, self.settings.avatar_dir
            )
        except Exception as exc:
            logger.warning("avatar fetch at session start failed: %s", exc)

    def _close_signer_client_later(self) -> None:
        """前回の署名用 httpx クライアントを閉じる。aclose() はコルーチンなので
        イベントループ上で走らせる。閉じられなくても致命的ではない
        (GCで回収される)ため、失敗しても無視する。"""
        old = self._signer_http_client
        self._signer_http_client = None
        if old is None:
            return
        try:
            asyncio.get_event_loop().create_task(old.aclose())
        except Exception:
            pass

    def current_room_id(self) -> str | None:
        """TikTokLiveClient が接続時に埋める room_id。接続前は None。
        文字列に正規化する -- DB の room_id 列は TEXT で、int と str が
        混ざると比較が成立しなくなる。"""
        raw = getattr(self.client, "room_id", None) if self.client is not None else None
        return str(raw) if raw else None

    def _start_watchdog(self) -> None:
        self.last_event_at = time.monotonic()
        self.watchdog = IdleWatchdog(self.settings.idle_timeout_sec, self._on_idle_timeout)
        self.watchdog.start()

    def note_event(self) -> None:
        """イベント受信を記録する。watchdog のリセットと、監督ループが読む
        last_event_at の更新をまとめて行う。"""
        self.last_event_at = time.monotonic()
        if self.watchdog:
            self.watchdog.notify_event()

    def end_now(self, end_detection_type: str) -> None:
        """明示的なシグナルによる即時終了。生存再確認を挟まない --
        TikTok が『終わった』と言っている以上、確認する必要がない。"""
        if self.live_session_id is None or self._ended:
            return
        logger.info(
            "session ended by %s: id=%s (@%s)",
            end_detection_type, self.live_session_id, self.settings.username,
        )
        db.end_session(self.conn, self.live_session_id, end_detection_type)
        self._ended = True
        self.live_ended_signal = end_detection_type
        self.live_session_id = None
        if self.watchdog:
            self.watchdog.stop()
        if self._screenshot_task and not self._screenshot_task.done():
            self._screenshot_task.cancel()

    async def _on_idle_timeout(self) -> None:
        # 旧仕様ではここでセッションを終了していたが、それが1本の配信を
        # 複数セッションに割る原因だった。現在は監督ループ
        # (proxy_pool_trial.supervise_loop)が last_event_at を見て、別IPで
        # is_live + room_id を確認してから継続/終了を決める。ここは何もしない。
        logger.debug(
            "no events for %.0fs on session id=%s -- leaving the decision to the supervisor",
            self.settings.idle_timeout_sec, self.live_session_id,
        )
        return

    async def _unused_on_idle_timeout(self) -> None:
        db.end_session(self.conn, self.live_session_id, "auto")
        self._ended = True
        self.live_session_id = None
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    def _record_event(self, normalizer, raw_event) -> None:
        self._ensure_session()
        if self.live_session_id is None:
            return  # 終了済み -- 遅延イベントは捨てる(_ensure_session 参照)
        self.note_event()
        event_type, user_id, user_nickname, payload = normalizer(raw_event)
        raw_payload = event_normalizers.safe_serialize(raw_event)
        occurred_at = event_normalizers.extract_occurred_at(raw_event)
        try:
            db.insert_event(
                self.conn,
                self.live_session_id,
                event_type,
                user_id,
                user_nickname,
                payload,
                raw_payload,
                occurred_at=occurred_at,
            )
        except db.WriteFailed as exc:
            # **捨てない。** 再試行しても書けなかったので、ファイルへ退避する。
            logger.error(
                "DBに書けなかったイベントを %s へ退避: type=%s @%s (%s)",
                FAILED_EVENT_LOG_PATH, event_type, self.settings.username, exc,
            )
            _record_failed_event({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": str(exc),
                "username": self.settings.username,
                "live_session_id": self.live_session_id,
                "event_type": event_type,
                "user_id": user_id,
                "user_nickname": user_nickname,
                "payload": payload,
                "raw_payload": raw_payload,
                "occurred_at": occurred_at,
            })
            return
        logger.debug("event=%s user=%s payload=%s", event_type, user_nickname, payload)

    def _handle_battle_event(self, raw_event) -> None:
        self._ensure_session()
        if self.live_session_id is None:
            return  # 終了済み -- 遅延イベントは捨てる
        self.note_event()
        raw_payload = event_normalizers.safe_serialize(raw_event)
        occurred_at = event_normalizers.extract_occurred_at(raw_event)
        candidates = event_normalizers.find_display_ids(raw_payload)
        opponents = event_normalizers.filter_battle_opponents(candidates, self.settings.username)
        source_event_type = type(raw_event).__name__

        for opponent_id in opponents:
            if opponent_id in self._recorded_opponents:
                continue
            self._recorded_opponents.add(opponent_id)
            db.insert_event(
                self.conn,
                self.live_session_id,
                "battle_opponent",
                opponent_id,
                None,
                {"opponent_id": opponent_id, "source_event_type": source_event_type},
                raw_payload,
                occurred_at=occurred_at,
            )
            logger.info("battle opponent detected: %s (via %s)", opponent_id, source_event_type)

    def _handle_envelope_event(self, raw_event) -> None:
        """WebcastEnvelopeMessage carries both Treasure Box and TikTok's
        unrelated Red Envelope feature -- see events.py's Treasure Box
        comment. Parsing is wrapped defensively so a future field-shape
        change skips just this one box (logged) rather than taking down
        event recording."""
        self._ensure_session()
        if self.live_session_id is None:
            return  # 終了済み -- 遅延イベントは捨てる
        self.note_event()
        try:
            if not event_normalizers.is_treasure_box_envelope(raw_event):
                return
            event_type, user_id, user_nickname, payload = event_normalizers.normalize_treasure_box_envelope(raw_event)
        except Exception as exc:
            logger.warning("treasure box (envelope) parsing failed, skipping: %r", exc)
            return
        raw_payload = event_normalizers.safe_serialize(raw_event)
        occurred_at = event_normalizers.extract_occurred_at(raw_event)
        db.insert_event(
            self.conn,
            self.live_session_id,
            event_type,
            user_id,
            user_nickname,
            payload,
            raw_payload,
            occurred_at=occurred_at,
        )
        logger.info("treasure box detected: coins=%s sender=%s", payload.get("coins"), user_nickname)

    def build_client(
        self,
        on_status: StatusCallback | None = None,
        sign_quota_state: dict | None = None,
    ) -> TikTokLiveClient:
        # Logged every call (i.e. every connect/reconnect attempt) so a
        # measurement run's logs always show which IP path was actually
        # used -- see proxy.py for what is/isn't covered by this.
        proxy_config = proxy_module.load_proxy_config(self.settings.proxy_url)
        proxy = proxy_module.build_httpx_proxy(proxy_config)
        client = TikTokLiveClient(unique_id=self.settings.username, web_proxy=proxy, ws_proxy=proxy)
        self.client = client
        # 署名も同じプロキシから出す。録画1本ぶんの署名消費が、その録画が
        # 使っているIPの予算から引かれるので、10本のIPに自然に分散される。
        # 前回の接続で作ったクライアントはここで閉じる(再接続のたびに
        # httpx.AsyncClient が積み上がるのを防ぐ)。
        self._close_signer_client_later()
        self._signer_http_client = _route_signer_through_proxy(client, self.settings.proxy_url)
        # Requires TikTokLive>=6.6.6 -- client.parse_error_ignorelist doesn't
        # exist on 6.6.5 or earlier (AttributeError), confirmed by diffing
        # the installed package across versions. requirements.txt pins this
        # floor; if you hit that AttributeError here, you're running with a
        # stale/wrong interpreter (e.g. a global Python install instead of
        # this project's .venv), not a real incompatibility to code around.
        #
        # Confirmed-harmless upstream schema bugs: TikTokLive's own
        # client.parse_error_ignorelist demotes a payload-parse failure from
        # ERROR+traceback to a one-line DEBUG when str(exception) matches
        # one of these substrings (see client.py's parse_webcast_response in
        # the installed TikTokLive package) -- doesn't touch any other
        # logging, ours included. WebcastLinkLayerMessage failing to parse
        # its HashtagNamespace field doesn't affect comment/gift/treasure-
        # box recording (verified: unrelated message type).
        client.parse_error_ignorelist.extend(_KNOWN_HARMLESS_PARSE_ERRORS)
        # NOT confirmed harmless -- demoted from the noisy default only
        # because _attach_gift_parse_failure_capture (below) captures the
        # raw payload first. See _KNOWN_LOSSY_PARSE_ERRORS.
        client.parse_error_ignorelist.extend(_KNOWN_LOSSY_PARSE_ERRORS)
        _attach_gift_parse_failure_capture(client, username=self.settings.username)
        _attach_sign_rate_limit_logger(client, quota_state=sign_quota_state)

        @client.on(ConnectEvent)
        async def on_connect(_event: ConnectEvent) -> None:
            room_id = self.current_room_id()
            logger.info("connected to @%s (room_id=%s)", self.settings.username, room_id)
            self._ensure_session()
            # 接続時点で判明した room_id を、まだ空なら書き込む。セッションが
            # room_id 無しで作られた直後(イベント先行)でも埋まるようにする。
            if self.live_session_id is not None:
                db.set_session_room_id(self.conn, self.live_session_id, room_id)
            _notify(on_status, "connected", username=self.settings.username, room_id=room_id)

        @client.on(DisconnectEvent)
        async def on_disconnect(_event: DisconnectEvent) -> None:
            logger.warning("disconnected from @%s", self.settings.username)
            _notify(on_status, "disconnected", username=self.settings.username)

        @client.on(EnvelopeEvent)
        async def on_envelope(event: EnvelopeEvent) -> None:
            self._handle_envelope_event(event)

        # --- ライブの明示的な終了/一時停止 ---------------------------------
        # 2026-09-01 の観測で、配信終了時に届くシグナルは2経路あることを
        # 実データで確認した(観測5件):
        #   経路A: LiveEndEvent が届き、その1秒以内に切断される
        #   経路B: LiveEndEvent は届かず、WebSocket が NORMAL_CLOSURE(1000)
        #          で正常終了する
        # どちらか一方だけでは取りこぼすので両方を終了シグナルとして扱う。
        # 経路Bは run_with_reconnect 側で拾う(ここは経路A)。
        if _LIVE_END_EVENT_CLS is not None:
            @client.on(_LIVE_END_EVENT_CLS)
            async def on_live_end(_event) -> None:
                logger.info("LiveEndEvent received for @%s", self.settings.username)
                _notify(on_status, "live_end", username=self.settings.username)
                self.end_now("live_end")

        # LivePause 中は無イベントが正常。実測で80秒の一時停止を観測しており
        # (haru04150728, 2026-09-01 15:36:47-15:38:07)、無イベント検知の
        # 1分をゆうに超える。paused の間は監督ループが生存確認を見送る。
        if _LIVE_PAUSE_EVENT_CLS is not None:
            @client.on(_LIVE_PAUSE_EVENT_CLS)
            async def on_live_pause(_event) -> None:
                logger.info("LivePauseEvent: @%s paused", self.settings.username)
                _notify(on_status, "live_paused", username=self.settings.username)
                self.paused = True
                self.note_event()

        if _LIVE_UNPAUSE_EVENT_CLS is not None:
            @client.on(_LIVE_UNPAUSE_EVENT_CLS)
            async def on_live_unpause(_event) -> None:
                logger.info("LiveUnpauseEvent: @%s resumed", self.settings.username)
                _notify(on_status, "live_unpaused", username=self.settings.username)
                self.paused = False
                self.note_event()

        # --- 観測専用(2026-09-01 調査) ------------------------------------
        # セッションが1本のライブで複数に割れる問題の調査用。現在の終了判定は
        # idle timeout(既定60秒の無イベント)だけで、TikTokが明示する終了を
        # 一切見ていない。LiveEndEvent 等が実際にどれくらい届くのかを実データ
        # で確かめるまで判定は変えられないので、まず「記録するだけ」を入れる。
        #
        # 挙動は意図的に一切変えていない:
        #   - watchdog.notify_event() を呼ばない(呼ぶとidleタイマーが延び、
        #     現行の終了タイミングが変わってしまい観測にならない)
        #   - _ensure_session() を呼ばない(新規セッションを作らない)
        #   - DBにも書かない
        # ログと on_status(= events.jsonl)にだけ出す。
        for control_cls in _OBSERVED_CONTROL_EVENT_CLASSES:
            def make_control_handler(control_cls=control_cls):
                async def handler(event) -> None:
                    concrete = type(event).__name__
                    detail = event_normalizers.safe_serialize(event)
                    logger.info(
                        "CONTROL_OBSERVED cls=%s (listener=%s) user=@%s session=%s payload=%s",
                        concrete, control_cls.__name__, self.settings.username,
                        self.live_session_id, str(detail)[:500],
                    )
                    _notify(
                        on_status, "control_observed",
                        username=self.settings.username,
                        event_class=concrete,
                        listener_class=control_cls.__name__,
                        live_session_id=self.live_session_id,
                        payload=str(detail)[:1000],
                    )

                return handler

            client.add_listener(control_cls, make_control_handler())

        for event_cls, normalizer in _EVENT_NORMALIZERS.items():
            def make_handler(normalizer=normalizer):
                async def handler(event) -> None:
                    self._record_event(normalizer, event)

                return handler

            client.add_listener(event_cls, make_handler())

        for event_cls in _BATTLE_EVENT_CLASSES:
            def make_battle_handler():
                async def handler(event) -> None:
                    self._handle_battle_event(event)

                return handler

            client.add_listener(event_cls, make_battle_handler())

        return client

    async def stop_for_handoff(self) -> None:
        """IP乗り換えのために接続だけ切る。**セッションは閉じない** --
        同じライブを別IPで録り続けるので、live_sessions の行はそのまま
        引き継ぎ先が使う。end_session を呼ばないのが manual_end との違い。"""
        logger.info(
            "stopping connection for handoff: session=%s (@%s)",
            self.live_session_id, self.settings.username,
        )
        self._ended = True          # run_with_reconnect のループを抜けさせる
        if self.watchdog:
            self.watchdog.stop()
        if self._screenshot_task and not self._screenshot_task.done():
            self._screenshot_task.cancel()
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    async def hard_stop(self, end_detection_type: str = "interrupted") -> None:
        """セッションを閉じ、**接続も切る**。

        end_now() は live_sessions の行を閉じるだけで WebSocket には触れない。
        配信が本当に終わっていれば TikTok 側が接続を閉じてくれるので、それで
        足りていた。しかし「接続が死んでいるのにサーバが閉じてこない」場合、
        run_with_reconnect は `await task` で待ち続け、録画タスクが終わらない
        ので **スロットが解放されない**。ハードタイムアウトはまさにその状態を
        畳むためのものなので、こちらから明示的に切りにいく。

        disconnect() 自体が固まる可能性がある(相手が黙っているのが前提の
        処理なので、無期限に待つのは筋が悪い)。待ち時間に上限を設ける。
        """
        self.end_now(end_detection_type)
        # end_now は「既に終了済み」だと何もせずに返る。その場合でも接続だけは
        # 確実に切りたいので、フラグと後始末はここで改めて立てる。
        self._ended = True
        if self.watchdog:
            self.watchdog.stop()
        if self._screenshot_task and not self._screenshot_task.done():
            self._screenshot_task.cancel()
        if self.client is not None:
            try:
                await asyncio.wait_for(
                    self.client.disconnect(), timeout=HARD_STOP_DISCONNECT_TIMEOUT_SEC
                )
            except Exception:
                logger.debug(
                    "disconnect during hard stop failed or timed out for @%s",
                    self.settings.username, exc_info=True,
                )

    def interrupted_end(self) -> None:
        """収集プロセスが止まったことによる終了。**配信が終わったわけではない**。

        manual_end() との違いは end_detection_type だけだが、その差が
        再開できるかどうかを決める: 'interrupted' は
        db.RESUMABLE_END_TYPES に含まれるので、収集プロセスを再起動して
        同じ room_id のライブにまだ繋がれば、同じセッションに書き戻せる。
        'manual'(人が明示的に終了した)は再開対象外。

        2026-09-01 の実例: SIGINT で停止したところ、稼働中の5セッションが
        'manual' で閉じられ、再起動後にすべて別セッションとして作り直された
        (mu_chan38 id=36→49 など)。配信は続いていたので、本来は継続すべき
        だった。watch.py / main.py は人が直接 Ctrl+C する経路なので
        manual_end() のままでよい -- あちらの 'manual' は事実に合っている。
        """
        if self.live_session_id is not None:
            db.end_session(self.conn, self.live_session_id, "interrupted")
            self._ended = True
            self.live_session_id = None
        if self.watchdog:
            self.watchdog.stop()
        if self._screenshot_task and not self._screenshot_task.done():
            self._screenshot_task.cancel()

    def manual_end(self) -> None:
        if self.live_session_id is not None:
            db.end_session(self.conn, self.live_session_id, "manual")
            self._ended = True
            self.live_session_id = None
        if self.watchdog:
            self.watchdog.stop()

    @property
    def ended(self) -> bool:
        return self._ended


CheckIsLiveFn = Callable[[str], "asyncio.Future"]  # async def fn(username: str) -> bool

# Ceiling on any single sleep between reconnect attempts, even when honoring
# a signature-rate-limit's own retry_after (which can be hours for an
# account_day hit) -- keeps a multi-day unattended run's logs showing signs
# of life periodically instead of one silent multi-hour gap.
MAX_SLEEP_SEC = 3600.0


async def run_with_reconnect(
    runner: "SessionRunner",
    settings: Settings,
    on_status: StatusCallback | None = None,
    sign_quota_state: dict | None = None,
    check_is_live_fn: CheckIsLiveFn | None = None,
    max_consecutive_failures: int | None = None,
    max_reconnects_per_live: int | None = None,
    max_initial_connect_failures: int | None = None,
) -> SessionRunner:
    """Runs the monitoring loop until the idle watchdog auto-ends the session
    or the caller cancels this coroutine (manual end via Ctrl+C).

    ``runner`` is constructed by the caller (outside asyncio.run) so that,
    on Windows, a KeyboardInterrupt that never reaches an ``await`` inside
    this coroutine (a known asyncio/selector quirk) still leaves the caller
    holding a live reference it can call ``manual_end()`` on.

    on_status/sign_quota_state are optional (Phase 5 concurrent-measurement
    prep, see StatusCallback above) -- both default to None, and main.py/
    watch.py never pass them, so single-streamer recording is unaffected.

    check_is_live_fn/max_consecutive_failures (Phase 5 signature-exhaustion
    prep, see the "kana18724 incident" postmortem): a streamer can report
    is_live=True via the HTTP status API while the WebSocket handshake
    itself keeps failing (observed as InvalidStatusCode(400)) -- a
    completely different TikTok endpoint from the one is_live checks.
    ``client.start()`` re-fetches a fresh Euler Stream signature on every
    single attempt regardless of whether the WS handshake that follows
    succeeds, so retrying such a streamer at a fixed short interval with no
    cap drains the account's signing quota (minute -> hour -> day) in
    minutes, taking every other concurrently-monitored streamer down with
    it once the quota is gone. max_consecutive_failures=None (the default)
    preserves the original unlimited-retry behavior for main.py/watch.py,
    where a human is watching and can Ctrl+C; Phase 5's orchestrator passes
    a finite cap and its own paced check_is_live_fn (routed through the
    same zero-burst pacing as pool scanning) so a broken streamer gets
    quarantined instead of burning the whole run's signing budget.

    max_reconnects_per_live (2026-08-27 signature-exhaustion postmortem,
    round two): a SEPARATE, independent counter from consecutive_failures
    above, because consecutive_failures resets to 0 on every genuine
    connect -- a streamer that connects fine, disconnects moments later,
    reconnects fine, disconnects again, and repeats that all night never
    trips max_consecutive_failures even once (each cycle "succeeds" before
    failing again), yet spends one signature per reconnect the whole time.

    reconnect_count only counts attempts that actually reached (or would
    have reached) client.start()'s fetch_signed_websocket step -- i.e. it
    passed the is_live check first. Confirmed by reading TikTokLive's
    client.start(): the is_live check happens BEFORE signing, so a plain
    UserOfflineError (offline_retry) never spends a signature at all, and
    counting it toward this cap was a real bug found in production
    (2026-08-28): a normal night with zero flapping still filled
    data/problematic_streamers.jsonl with streamers that had simply ended
    their broadcast, each wrongly quarantined for an hour and shrinking the
    healthy-streamer pool the whole experiment was trying to measure.
    SignatureRateLimitError is excluded from the count too, for the same
    reason it's excluded from consecutive_failures: it's an account-wide
    condition, not evidence this particular streamer is flaky. Every other
    outcome -- a clean disconnect-then-reconnect, or a connection_error
    (confirmed still live, so is_live already passed once for this
    attempt) -- counts, because each of those really did cost a signature.
    This also naturally covers the "connects, drops, comes back live
    moments later, drops again" pattern: the offline_retry legs are free
    and skipped, but each successful recovery reconnect still passed
    is_live and got counted, with no separate counter needed.

    Whichever cap (this one or max_consecutive_failures) is hit first ends
    the attempt; they can't both fire for the same exit since each returns
    immediately. Unlike max_consecutive_failures, this one doesn't require
    confirming is_live first: the point isn't "is this streamer's stream
    still going" (that's what the other cap protects against getting
    wrong), it's "this specific live has needed too many signature-costing
    reconnects" -- Phase 5's orchestrator quarantines the streamer for this
    one live/cooldown window, not permanently, on the theory that flakiness
    is often IP/network-moment-specific rather than a permanent property of
    the streamer (see phase5_measure.py's PROBLEM_STREAMER_COOLDOWN_SEC).

    max_initial_connect_failures (2026-09-02): a THIRD counter, and
    deliberately a separate one -- it applies **only while ever_connected is
    False**, i.e. the initial connection to a live we have never attached to.
    That path is a different failure mode from the two above, which both
    describe a live we did successfully record and then lost:

      @kyutyom, 2026-09-01: 13 consecutive InvalidStatusCode(400) at the
      WebSocket handshake, every one of them from the same proxy IP (#10),
      inside a single run_with_reconnect call. is_live kept answering True
      (a different TikTok endpoint), so the "confirmed still live" branch
      below kept retrying, and each retry re-fetched a signature. 13
      signatures spent, zero events recorded.

    max_consecutive_failures cannot cover this: the orchestrator leaves it
    None precisely because a mid-live blip should be retried in place rather
    than dropping a working recording. Capping the never-connected case
    separately lets the orchestrator hand the decision back to the pool,
    which knows things this function doesn't -- which IPs are free, and
    whether this (username, room_id) has already burned its budget (see
    proxy_pool_trial.InitialConnectGate)."""
    if check_is_live_fn is None:
        # Deferred import: watch.py imports run_with_reconnect from this
        # module, so a top-level "from .watch import check_is_live" here
        # would be circular. By the time this function actually runs, both
        # modules are fully loaded, so the deferred import is safe.
        from .watch import check_is_live as check_is_live_fn

    delay = settings.reconnect_initial_delay_sec
    ever_connected = False
    consecutive_failures = 0
    reconnect_count = -1  # the first attempt isn't a "reconnect"; becomes 0 after it, 1 after the next signature-costing attempt, etc.
    last_reconnect_error: str | None = None
    last_is_live_at_failure: bool | None = None

    while not runner.ended:
        connected_this_attempt = {"connected": False}
        # Whether THIS iteration's attempt reached (or would have reached)
        # fetch_signed_websocket -- i.e. it's worth counting toward
        # max_reconnects_per_live. Defaults True (a clean disconnect-then-
        # reconnect, or client.start() succeeding outright, both did reach
        # signing); the UserOfflineError and SignatureRateLimitError
        # branches below flip it False -- see this function's docstring.
        count_toward_reconnect_limit = True

        def _tracking_on_status(kind: str, info: dict, _flag=connected_this_attempt) -> None:
            if kind == "connected":
                _flag["connected"] = True
            _notify(on_status, kind, **info)

        client = runner.build_client(on_status=_tracking_on_status, sign_quota_state=sign_quota_state)
        sleep_override = None
        try:
            task = await client.start(fetch_live_check=True)
            await task
        except UserNotFoundError:
            logger.error("@%s does not exist or has never gone live. Exiting.", settings.username)
            _notify(on_status, "user_not_found", username=settings.username)
            return runner
        except UserOfflineError:
            if not ever_connected:
                logger.error("@%s is not currently live. Exiting.", settings.username)
                _notify(on_status, "user_offline_exit", username=settings.username)
                return runner
            logger.warning("stream reported offline, retrying in %.0fs", delay)
            _notify(on_status, "offline_retry", username=settings.username, delay=delay)
            last_reconnect_error = "offline (temporary, still reconnecting)"
            last_is_live_at_failure = False
            # is_live is checked BEFORE signing in client.start() -- this
            # attempt never reached fetch_signed_websocket, so it cost
            # nothing. Confirmed in production (2026-08-28): counting these
            # wrongly quarantined streamers that had simply ended their
            # broadcast, on a night with zero actual flapping.
            count_toward_reconnect_limit = False
        # Caught ahead of the generic Exception branch below so a Euler
        # Stream signing-quota hit is never lumped in with an actual TikTok-
        # side connection problem in the logs -- Phase 5 measurement needs
        # to be able to tell those two apart (see tiktok_monitor/proxy.py's
        # docstring for why signing isn't covered by the configured proxy).
        except SignatureRateLimitError as exc:
            # exc.retry_after is NOT usable as a wait duration: TikTokLive
            # 7.0.0's SignatureRateLimitError.calculate_retry_after() (see
            # .venv/Lib/site-packages/TikTokLive/client/errors.py) returns
            # int(response.headers["RateLimit-Remaining"]) -- the remaining
            # QUOTA COUNT, mislabeled as a retry-after seconds value. It was
            # observed as a constant 0 throughout the kana18724 incident
            # (quota already at 0), which would have made a sleep_override
            # based on it *worse* than the plain backoff it replaced.
            #
            # exc.reset_time is documented as "the unix timestamp for when
            # the client can request again", but the actual account_day
            # response observed in that incident behaved as a countdown in
            # seconds instead (it decreased by ~1 real second per elapsed
            # second across repeated polls, e.g. 14879 -> 14816 -> 14754
            # over ~63s each -- a fixed epoch value cannot do that). Rather
            # than trust either interpretation blindly, treat anything that
            # isn't plausibly "now" as epoch seconds (small values can't be
            # a valid current Unix timestamp, which is always > 1e9) as a
            # countdown from now instead.
            now = time.time()
            if exc.reset_time > 1_000_000_000:
                wait_seconds = max(exc.reset_time - now, 1.0)
                reset_at_ts = exc.reset_time
            else:
                wait_seconds = max(exc.reset_time, 1.0)
                reset_at_ts = now + exc.reset_time
            reset_at = datetime.fromtimestamp(reset_at_ts, tz=timezone.utc).isoformat()
            logger.warning(
                "signature server rate limit reached (Euler Stream, not TikTok) -- "
                "wait=%.0fs reset_at=%s (raw reset_time=%s, retry_after=%s -- see comment, not a real duration): %s",
                wait_seconds,
                reset_at,
                exc.reset_time,
                exc.retry_after,
                exc,
            )
            _notify(
                on_status,
                "signature_rate_limit",
                username=settings.username,
                retry_after=exc.retry_after,
                reset_time=exc.reset_time,
                wait_seconds=wait_seconds,
                message=str(exc),
            )
            # Euler Stream tells us roughly when the quota resets -- honor
            # that instead of our own short backoff, which would otherwise
            # keep hammering an already-exhausted quota every
            # reconnect_max_delay_sec seconds until it resets on its own.
            sleep_override = min(wait_seconds, MAX_SLEEP_SEC)
            last_reconnect_error = f"signature_rate_limit: {exc}"
            # is_live_at_failure deliberately left as whatever it was last --
            # this is a global signing-quota condition, not a fact about
            # whether THIS streamer is still live.
            #
            # Not counted toward max_reconnects_per_live either, for the
            # same reason it's excluded from consecutive_failures: an
            # account-wide quota condition isn't evidence THIS streamer is
            # flaky, so it shouldn't be what gets them quarantined.
            count_toward_reconnect_limit = False
        except KeyboardInterrupt:
            logger.info("keyboard interrupt received, ending session manually")
            runner.manual_end()
        except Exception as exc:
            # 経路B: LiveEndEvent が届かないまま WebSocket が NORMAL_CLOSURE(1000)
            # で閉じられるパターン。2026-09-01 の観測で実在を確認した
            # (baby_8_xo 74分 / aika._1029 28分。どちらも LiveEndEvent は届かず、
            # 直後に配信が終わっていた)。code=1000 は「サーバが意図して正常に
            # 閉じた」なので再接続せず終了確定にする。1000 以外(1006 異常切断
            # など)は本物の異常なので、この if を通り抜けて従来の再接続処理へ。
            if _is_normal_closure(exc):
                logger.info(
                    "websocket closed normally (NORMAL_CLOSURE) for @%s -- treating as live end",
                    settings.username,
                )
                _notify(on_status, "normal_closure", username=settings.username)
                runner.end_now("normal_closure")
                return runner
            # A failed WS handshake (e.g. InvalidStatusCode(400)) can mean
            # either "the stream just ended and TikTok didn't report that
            # cleanly this time" (not an anomaly -- nothing left to record)
            # or "still live, but this IP/session genuinely can't connect"
            # (a real problem worth quarantining). Re-checking is_live here,
            # once, is the only way to tell those apart instead of guessing
            # from retry counts alone -- see the kana18724 incident.
            is_live_now = await check_is_live_fn(settings.username)
            if not is_live_now:
                logger.info(
                    "connection error for @%s (%s), and the stream is now offline -- "
                    "treating as a natural end, not an anomaly",
                    settings.username,
                    exc,
                )
                _notify(
                    on_status,
                    "user_offline_exit",
                    username=settings.username,
                    detail="offline confirmed after a connection error",
                )
                return runner
            consecutive_failures += 1
            last_reconnect_error = repr(exc)
            last_is_live_at_failure = True
            logger.warning(
                "connection error (%s) for @%s while still live [%d%s consecutive], retrying in %.0fs",
                exc,
                settings.username,
                consecutive_failures,
                f"/{max_consecutive_failures}" if max_consecutive_failures is not None else "",
                delay,
            )
            _notify(
                on_status,
                "connection_error",
                username=settings.username,
                error=repr(exc),
                delay=delay,
                is_live_at_failure=True,
                consecutive_failures=consecutive_failures,
                ever_connected=ever_connected,
                # room_id は「どのライブに対する失敗か」を呼び出し側が
                # 区別するために要る。int/str が混ざると突き合わせが
                # 成立しないので str に正規化する(excluded_reconnect_limit と同じ)。
                room_id=str(client.room_id) if client.room_id else None,
            )
            # 一度も繋がっていない場合だけ効く上限。繋がった後の再接続
            # (= 録画実績のあるライブ)には一切影響しない。
            if (not ever_connected
                    and max_initial_connect_failures is not None
                    and consecutive_failures >= max_initial_connect_failures):
                logger.warning(
                    "@%s: initial connection failed %d time(s) (%s) -- handing the decision "
                    "back to the pool instead of retrying on this IP",
                    settings.username, consecutive_failures, exc,
                )
                _notify(
                    on_status,
                    "gave_up_initial_connect",
                    username=settings.username,
                    error=repr(exc),
                    error_type=type(exc).__name__,
                    consecutive_failures=consecutive_failures,
                    room_id=str(client.room_id) if client.room_id else None,
                )
                return runner
            if max_consecutive_failures is not None and consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "@%s: giving up after %d consecutive connection failures while still live "
                    "(likely IP/session-specific, not the stream ending) -- freeing this slot",
                    settings.username,
                    consecutive_failures,
                )
                _notify(
                    on_status,
                    "gave_up_repeated_failures",
                    username=settings.username,
                    consecutive_failures=consecutive_failures,
                    last_error=repr(exc),
                )
                return runner
        finally:
            try:
                if client.connected:
                    await client.disconnect()
            except Exception:
                pass

        if runner.ended:
            break

        if connected_this_attempt["connected"]:
            ever_connected = True
            consecutive_failures = 0

        # Independent of consecutive_failures -- see this function's
        # docstring for why a flapping streamer (connects, drops, repeats)
        # never trips that counter but still needs a limit. Only counts
        # attempts that actually cost a signature (count_toward_reconnect_
        # limit, set per-branch above) -- a free offline_retry doesn't put
        # this streamer any closer to being excluded.
        if count_toward_reconnect_limit:
            reconnect_count += 1
            if max_reconnects_per_live is not None and reconnect_count >= max_reconnects_per_live:
                logger.error(
                    "@%s: excluded from this live after %d reconnect(s) (cap=%d) -- will retry on a future live",
                    settings.username,
                    reconnect_count,
                    max_reconnects_per_live,
                )
                _notify(
                    on_status,
                    "excluded_reconnect_limit",
                    username=settings.username,
                    reconnect_count=reconnect_count,
                    last_error=last_reconnect_error,
                    is_live_at_failure=last_is_live_at_failure,
                    # str に正規化する。connected 側(runner.current_room_id())は
                    # 文字列で通知しており、同じ events.jsonl に int と str が
                    # 混ざると room_id での突き合わせが成立しない。
                    room_id=str(client.room_id) if client.room_id else None,
                )
                return runner

        try:
            await asyncio.sleep(sleep_override if sleep_override is not None else delay)
        except KeyboardInterrupt:
            logger.info("keyboard interrupt received, ending session manually")
            runner.manual_end()
            break
        delay = (
            settings.reconnect_initial_delay_sec
            if connected_this_attempt["connected"]
            else min(delay * 2, settings.reconnect_max_delay_sec)
        )

    return runner
