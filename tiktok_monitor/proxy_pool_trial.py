"""Multi-IP round-robin proxy trial (2026-08-31, proxy5.net/Decodo IP-pool
evaluation). Cycles through a pool of proxy URLs (one distinct outbound IP
per line in --proxies-file), checking is_live for a small pool of known-
active test streamers (--pool-file), one check per tick using a DIFFERENT
IP each time -- paced globally by --check-pace-sec via the same CheckPacer
watch.py uses, so "how fast can we rotate IPs before TikTok blocks us" is
directly parameterized (1.0 for an aggressive free-trial probe, 5.0 to
match this project's established safe rate for the paid tier).

The moment a check finds someone live, that IP commits to recording them
(removed from the round-robin -- "in use") while the round-robin continues
over the remaining IPs. This enforces 1 IP = 1 concurrent recording (TikTok
kicks the earlier session on a repeat from the same IP otherwise) without
needing a fixed target_count -- however many of the pool's streamers happen
to be live at once, that many IPs end up recording, up to pool size.

Built as a standalone script rather than extending phase5_measure.py's
Orchestrator: that class's step-schedule/stability-hour/quarantine
machinery is irrelevant here and touching it risks regressing its existing,
separately-relied-upon behavior (CPU/memory measurement, anomaly tracking
for the current single-proxy pool-scan model). This reuses the same proven
lower-level building blocks instead (CheckPacer, check_is_live,
SessionRunner, run_with_reconnect) -- each already supports a per-call/
per-instance distinct proxy, so nothing in client.py or watch.py needed to
change for this.

Entirely independent of the 528-account measure-only run: separate
--db-path, separate --events-path, no shared state, no shared proxy.

Known gap NOT addressed here (separate issue, flagged 2026-08-31): avatar
fetching on session start (client.py's _maybe_fetch_avatar) always goes out
on the real IP regardless of the recording's own proxy -- fetch_avatars.py
takes no web_proxy parameter. Out of scope for this trial; the trial's own
is_live checks and websocket recording connections DO correctly use each
slot's assigned proxy either way.

Run:
    python -m tiktok_monitor.proxy_pool_trial \\
        --proxies-file data/proxy_pool_trial/proxy5_ips.txt \\
        --pool-file data/proxy_pool_trial/test_accounts.txt \\
        --check-pace-sec 1.0 \\
        --db-path data/proxy_pool_trial/proxy5.db \\
        --events-path data/proxy_pool_trial/proxy5_events.jsonl
"""
import argparse
import asyncio
import collections
import dataclasses
import json
import logging
import os
import time
from datetime import datetime, timezone

from . import db
from . import proxy as proxy_module
from .client import SessionRunner, run_with_reconnect
from .config import Settings
from . import watch as watch_module
from .watch import CheckPacer, _read_usernames_file

logger = logging.getLogger(__name__)

DEFAULT_CHECK_PACE_SEC = 1.0
DEFAULT_DB_PATH = "data/proxy_pool_trial.db"
DEFAULT_EVENTS_PATH = "data/proxy_pool_trial_events.jsonl"
HEARTBEAT_INTERVAL_SEC = 30.0
# How long to idle-wait when there's nothing to check right now (every
# slot busy recording, or every pool username already being recorded) --
# not part of the check-rate itself (CheckPacer owns that), just avoids a
# tight busy-loop while waiting for a slot/username to free up.
IDLE_WAIT_SEC = 0.5

# 10本中つねに1本は録画に使わず空けておき、巡回(新配信探し)と、録画中
# ライブの生存確認に使う。固定のIPを予約するのではなく「最低1本は空いている
# 状態を保つ」だけなので、確認に使うIPはその時々で変わる。
# これが無いと全IPが録画で埋まった瞬間に生存確認ができなくなり、ストールした
# 接続を検知できないまま放置することになる(実測の同時録画ピークは8本なので
# 9本枠で足りる)。
RESERVED_CHECK_SLOTS = 1

# 監督ループの周期。無イベント検知そのものの閾値ではない。
SUPERVISE_INTERVAL_SEC = 5.0

# 生存確認のトリガーとなる無イベント秒数。ここで終了はさせない。
STALL_THRESHOLD_SEC = 60.0

# 生存確認が UNKNOWN(確認できなかった)だった場合、何回連続したら
# エラーとして報告するか。**UNKNOWN では絶対にセッションを終了させない** --
# 「オフラインだった」のではなく「確認できなかった」だけなので、終了の
# 根拠にはならない。実測(2026-09-02)では巡回チェックの is_live=False の
# 3.0% がタイムアウト等の確認失敗で、これを終了根拠にすると録画中の
# セッションを誤って閉じることになる。
MAX_UNKNOWN_STREAK = 3

# --- 削除済み/配信不可アカウントの自動隔離(2026-09-02)---------------------
# streamers.txt には、削除された/一度も配信していないアカウントが残る。
# それらは巡回のたびに UserNotFoundError を返し、1周ごとに1回ずつ無駄打ち
# される(実測: riria0069 が22回、他3件を合わせて41回 = 全チェックの0.8%)。
# チェック自体は署名を使わないので致命的ではないが、プールの枠と時間を
# 食い続けるので、規定回数を超えたら巡回対象から外す。
#
# **恒久的な除外にはしない**。TikTok 側の一時的な不調や、アカウントが後で
# 復活する可能性があるため、プロセスを再起動すれば隔離は解けるメモリ上の
# 措置にとどめる。streamers.txt 自体は書き換えない -- 入力ファイルを
# プログラムが勝手に書き換えると、ユーザーの編集と競合する。
QUARANTINE_AFTER_NOT_FOUND = 3          # UserNotFoundError がこの回数に達したら隔離

# --- 長時間ストールガード(2026-09-02)-------------------------------------
# 短い無イベント(60秒)では何もしないが、**本当に接続が死んだ場合**に
# 放置しないための2段目。ここに達したら1回だけIPを変えて繋ぎ直す。
# (最後の砦は3段目の HARD_TIMEOUT_SEC。あちらは署名を使わない)
#
# 再接続には必ず署名を1つ消費する。署名URLの再利用は不可能であることを
# 確認済み -- TikTokLive の ws_connect.py に「signed URLs expire after 30
# seconds」と明記され、そのためライブラリ自身が再接続機構を無効化している。
# よって「繋ぎ直す = 署名を使う」は避けられない。だから安売りしない。
# 閾値の根拠(2026-09-02 実測 / 当初の 600 秒から引き上げ):
# 「接続を保ったままイベントが止まり、その後戻った」ギャップを全数集計した
# (乗り換え・繋ぎ直し・録画やり直しを挟んだものは除外。125件):
#     中央値 84.8s / p90 182.3s / p95 221.5s / p99 543.0s / **最大 834.5s**
# 600秒では最大値(834.5s)を下回っており、待てば戻るものを繋ぎ直していた。
# 実際 600秒運用中に発火した2件(mu_chan38 661s / nyaaaan__26 660s)は
# どちらもこの分布の裾に収まる長さで、放っておけば戻った可能性が高い。
# 実測最大の2倍(1669s)を切り上げて 1680 秒とする。
LONG_STALL_THRESHOLD_SEC = 1680.0       # 28分間まったくイベントが無い
MAX_STALL_RECONNECTS_PER_SESSION = 1    # 1セッションにつき1回まで
MAX_STALL_RECONNECT_RETRIES = 2         # その1回の中でのリトライ上限
STALL_RECONNECT_COOLDOWN_SEC = 1800.0   # 30分。連続で繋ぎ直さない

# --- 初回接続のリトライ上限(2026-09-02)-------------------------------------
# **ストール再接続(MAX_STALL_RECONNECT_RETRIES)とは別の経路**。
# あちらは「一度は繋がって録れていたセッションが無音になった」ときの繋ぎ直し。
# こちらは「そもそも一度も繋がらない」ときの試行制限で、数える対象も上限も
# 別に持つ。実際、ストール側の上限をいくら絞っても下の事例は止まらなかった:
#
#   @kyutyom (2026-09-01): WSハンドシェイクで InvalidStatusCode(400) が
#   13回連続。**13回すべて同じIP(#10)から**。is_live は別エンドポイントなので
#   True を返し続け、run_with_reconnect は「まだ配信中なのだから再試行」と
#   判断して回り続けた。署名13個を消費してイベント0件。
#
# 全体では connection_error 60件のうち13件(22%)がこの1人。ここを潰すと
# 実測 1.25署名/ライブ が 1.05〜1.10 まで下がる。
MAX_INITIAL_CONNECT_ATTEMPTS = 2        # 同一 (username, room_id) への接続試行の上限
INITIAL_CONNECT_COOLDOWN_SEC = 1800.0   # 上限に達したら30分あけない
# 1回の dispatch の中で run_with_reconnect に許す初回接続失敗の回数。
# 1にすることで「1 dispatch = 1試行 = 署名1つ」が成立し、上の上限が
# そのまま「同一ライブに使う署名の上限」になる。
INITIAL_CONNECT_FAILURES_PER_DISPATCH = 1
# InvalidStatusCode(400) は WSハンドシェイクの拒否で、kyutyom の実例が示すとおり
# IP/セッション固有の症状として出る。同じIPで繰り返しても直らないので、
# この種のエラーの直後の再試行は必ず別IPに振る。
_IP_SPECIFIC_CONNECT_ERRORS = ("InvalidStatusCode",)


class InitialConnectGate:
    """初回接続の失敗を (username, room_id) 単位で数え、上限に達したら
    一定時間そのライブへの接続試行を止める門番。

    **ストールガードとは独立**。ProxySlot が持つ stall_reconnects /
    stall_retries は「録れていたセッション」の話で、こちらは接続が成立
    していないので、そもそも紐づくセッションが無い。混ぜてはいけない。

    room_id の扱い: 最初の1回は room_id を知らないまま接続しにいくので、
    突き合わせは「失敗時に判明した room_id」で行う。別の room_id の失敗が
    来たら新しいライブなので、回数を数え直す(前のライブの失敗を新しい
    ライブに持ち越さない)。逆に、クールダウン中は接続しないので room_id を
    知る機会が無く、その30分の間に始まった別のライブも巻き添えで見送る。
    これは承知のうえの取引 -- 相手は直前まで接続できなかったライバーで、
    見送りのコストは署名0、取りこぼしても次の巡回で拾える。
    """

    def __init__(self):
        self._state: dict[str, dict] = {}

    def blocked_until(self, username: str, now: float) -> float | None:
        """クールダウン中なら明ける時刻を返す。対象外なら None。"""
        st = self._state.get(username)
        if st is None or st["cooldown_until"] <= now:
            return None
        return st["cooldown_until"]

    def avoid_ip_index(self, username: str) -> int | None:
        """次の試行で避けるべきIP番号(直前の失敗がIP固有の症状だった場合)。"""
        st = self._state.get(username)
        if st is None or not st["ip_specific"]:
            return None
        return st["last_ip_index"]

    def record_failure(self, username: str, room_id: str | None,
                       error_type: str, ip_index: int, now: float) -> dict:
        st = self._state.get(username)
        if st is None or (room_id is not None and st["room_id"] is not None
                          and room_id != st["room_id"]):
            # 初回、または別のライブ(room_id が変わった)。数え直す。
            st = {"room_id": room_id, "attempts": 0, "cooldown_until": 0.0,
                  "last_ip_index": None, "ip_specific": False}
            self._state[username] = st
        if room_id is not None:
            st["room_id"] = room_id
        st["attempts"] += 1
        st["last_ip_index"] = ip_index
        st["ip_specific"] = any(k in error_type for k in _IP_SPECIFIC_CONNECT_ERRORS)
        if st["attempts"] >= MAX_INITIAL_CONNECT_ATTEMPTS:
            st["cooldown_until"] = now + INITIAL_CONNECT_COOLDOWN_SEC
        return st

    def record_success(self, username: str) -> None:
        """繋がったら白紙に戻す。失敗の記憶を引きずらない。"""
        self._state.pop(username, None)


# --- 3段目: ハードタイムアウト(2026-09-02)--------------------------------
# 1段目(60秒で生存確認)と2段目(長時間ストールで1回だけ繋ぎ直し)は、どちらも
# **外部条件が揃わないと発火しない**:
#   - 生存確認は「空きIPがある」ことが前提。全IPが録画中なら即 return する。
#   - 繋ぎ直しは is_live=LIVE の返事が前提。UNKNOWN が続けば一度も走らない。
#   - 繋ぎ直しに失敗した場合、クールダウン(30分)の間ずっと再試行しない。
# その間セッションは status='live' のままスロットを占有し続ける。実際に
# session 50 が143分 live のまま放置された事例がある。
#
# そこで最後の砦として、**最終イベントからの経過時間だけ** を見て畳む段を
# 置く。is_live も unknown_streak も参照しない -- 参照しないことが要点で、
# 上の2段が動けない状況こそ、この段が必要な状況だから。
#
# 畳む処理そのものは署名を消費しない(DBの行を閉じて接続を切るだけ)。
# 配信がまだ続いていた場合は巡回が拾い直して署名を1つ使うが、それは
# 「45分無音のスロットを空ける」対価として妥当で、しかも
# find_resumable_session が room_id 一致で **同じセッション行を再開する**
# ので、セッションが割れることはない(resume_window_sec も 45分)。
#
# 閾値の根拠(2026-09-02 実測): 接続を保ったままの自然回復は最大 834.5 秒。
# 2700秒はその約3.2倍で、かつ2段目(1680秒)から17分の猶予がある。
HARD_TIMEOUT_SEC = 2700.0
# 接続を切っても録画タスクが終わらないことがある。スロット解放が目的なので
# 猶予後もタスクが生きていればキャンセルする。
HARD_STOP_GRACE_SEC = 30.0

# 無イベント時に **IPを乗り換えるかどうか**(2026-09-02 に既定を False に変更)。
#
# 当初は「接続がストールした可能性があるので新しいIPに乗り換えてデータ取得を
# 復活させる」設計だったが、1日分の実データがその前提を否定した:
#
#   ストール検知 17件中、その後イベントが戻った 17件 / 戻らなかった 0件
#   クールダウンで乗り換えを見送った 8件も、すべて自然回復した
#
# つまり無イベントは「接続が死んだ」のではなく「たまたま静かだった」だけで、
# 待てば戻る。にもかかわらず乗り換えは接続をやり直すので:
#   - 署名を1つ消費する(全接続の11%が乗り換え由来だった)
#   - 再接続の間、確実にデータが途切れる
#   - 乗り換え先が接続できないと孤児セッションが生まれる(実例あり)
#
# そこで既定を「乗り換えず、別IPで is_live を確認するだけ」に変える。
# 確認は署名を消費しないので何度でも安全に行える。ライブが終わっていれば
# そこで終了確定でき、続いていれば既存の接続をそのまま待つ。
#
# 接続が本当に死んだまま戻らないケースが将来観測されたら、この値を True に
# 戻せば従来の乗り換え動作になる(クールダウンと上限もそのまま効く)。
HANDOFF_ENABLED = False

# 乗り換えを有効にした場合の制約。署名枯渇を防ぐために外せない
# (2026-08-27 の署名枯渇ポストモーテム参照)。
HANDOFF_COOLDOWN_SEC = 300.0
MAX_HANDOFFS_PER_SESSION = 8


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


def _read_proxy_urls_file(path: str) -> list[str]:
    """One proxy URL (http://user:pass@host:port) per line; blank lines and
    #-comments ignored; duplicates collapsed (first occurrence wins,
    preserving order). Mirrors watch.py's _read_usernames_file."""
    urls: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _masked(proxy_url: str) -> str:
    """host:port only, matching proxy.py's ProxyConfig.masked -- never log
    credentials, even to a local trial log file."""
    try:
        return proxy_module.parse_proxy_url(proxy_url).masked
    except ValueError:
        return "invalid"


@dataclasses.dataclass
class ProxySlot:
    index: int  # 1-based, for a human-readable "何本目"
    proxy_url: str
    in_use: bool = False
    username: str | None = None
    task: "asyncio.Task | None" = None
    runner: "SessionRunner | None" = None
    started_at: float | None = None  # time.monotonic() when recording started
    room_id: str | None = None            # 録画中ライブの room_id(継続判定の主キー)
    live_session_id: int | None = None    # 乗り換え時に引き継ぐセッションID
    handoff_count: int = 0                # このライブで何回IPを乗り換えたか
    last_handoff_at: float = 0.0          # 直近の乗り換え時刻(クールダウン管理)
    handing_off: bool = False             # 乗り換え処理中(二重起動の防止)
    unknown_streak: int = 0               # 生存確認が UNKNOWN だった連続回数
    stall_reconnects: int = 0             # 長時間ストールで繋ぎ直した回数(1セッション上限)
    stall_retries: int = 0                # その繋ぎ直しの中でのリトライ回数
    last_stall_reconnect_at: float = 0.0  # 直近の繋ぎ直し時刻(クールダウン)
    hard_stopping: bool = False           # ハードタイムアウトで畳んでいる最中(二重起動の防止)


class ProxyPoolTrial:
    def __init__(
        self,
        conn,
        proxy_urls: list[str],
        pool: list[str],
        settings_kwargs: dict,
        pacer: CheckPacer,
        events_path: str,
    ):
        self.conn = conn
        self.slots = [ProxySlot(index=i + 1, proxy_url=url) for i, url in enumerate(proxy_urls)]
        self.pool = pool
        self.settings_kwargs = settings_kwargs
        self.pacer = pacer
        self.events_path = events_path
        self._slot_cursor = 0
        self._pool_cursor = 0
        # username -> UserNotFoundError の連続回数。閾値に達したら巡回から外す。
        self._not_found_counts: collections.Counter = collections.Counter()
        self._quarantined: set[str] = set()
        # 初回接続の門番(ストールガードとは独立。上のクラスの docstring 参照)
        self._connect_gate = InitialConnectGate()
        self._trial_started_at = time.monotonic()
        self.max_concurrent_seen = 0

    def _log(self, event: str, **fields) -> None:
        _append_jsonl(
            self.events_path,
            {
                "timestamp": _now_iso(),
                "elapsed_sec": round(time.monotonic() - self._trial_started_at, 1),
                "event": event,
                **fields,
            },
        )

    def _free_slot_count(self) -> int:
        return sum(1 for s in self.slots if not s.in_use)

    def _borrow_check_slot(self) -> ProxySlot | None:
        """生存確認・巡回チェックに使う空きスロットを1つ返す(占有はしない)。
        録画には使わないので予約枠の制限を受けない。"""
        for _ in range(len(self.slots)):
            slot = self.slots[self._slot_cursor % len(self.slots)] if self.slots else None
            self._slot_cursor = (self._slot_cursor + 1) % max(1, len(self.slots))
            if slot is not None and not slot.in_use:
                return slot
        return None

    def _reserved_slots(self) -> int:
        """実際に予約する本数。IPが予約数以下しか無い場合は予約しない --
        そうしないと録画が一切始まらなくなる(IP1本の構成で全チェックが
        スキップされ、events.jsonl すら書かれないのをテストで検出)。
        10本運用では常に 1 を返し、9本を録画に使う。"""
        return RESERVED_CHECK_SLOTS if len(self.slots) > RESERVED_CHECK_SLOTS else 0

    def _next_recording_slot(self) -> ProxySlot | None:
        """**録画用**の空きスロット。_reserved_slots() 本を必ず残すので、
        全IPが録画で埋まって生存確認ができなくなることがない。"""
        if self._free_slot_count() <= self._reserved_slots():
            return None
        return self._next_available_slot()

    def _next_available_slot(self) -> ProxySlot | None:
        """Round-robin over self.slots, skipping any currently recording.
        Safe if a slot was removed mid-run (invalid proxy URL, see
        run_forever) -- len(self.slots) is re-read fresh each call, and the
        cursor's modulo wraps correctly against whatever the current length
        is."""
        n = len(self.slots)
        if n == 0:
            return None
        self._slot_cursor %= n  # a slot removed since the cursor last advanced can leave it stale
        for _ in range(n):
            slot = self.slots[self._slot_cursor]
            self._slot_cursor = (self._slot_cursor + 1) % n
            if not slot.in_use:
                return slot
        return None  # every IP is currently recording

    def _next_candidate_username(self) -> str | None:
        """Round-robin over self.pool, skipping any username already being
        recorded by some other slot right now."""
        n = len(self.pool)
        if n == 0:
            return None
        in_progress = {s.username for s in self.slots if s.in_use}
        for _ in range(n):
            username = self.pool[self._pool_cursor]
            self._pool_cursor = (self._pool_cursor + 1) % n
            if username in in_progress or username in self._quarantined:
                continue
            if self._connect_gate.blocked_until(username, time.monotonic()) is not None:
                continue        # 初回接続に連続で失敗した直後。30分あける
            return username
        return None  # every pool username is already being recorded somewhere

    def _make_on_status(self, slot: ProxySlot):
        def on_status(kind: str, info: dict) -> None:
            self._log(
                "status",
                ip_index=slot.index,
                proxy=_masked(slot.proxy_url),
                username=slot.username,
                kind=kind,
                info=info,
            )
            username = slot.username
            if username is None:
                return
            if kind == "connected":
                # 繋がった時点で初回接続の失敗履歴は無意味になる。
                self._connect_gate.record_success(username)
            elif kind == "gave_up_initial_connect":
                st = self._connect_gate.record_failure(
                    username,
                    room_id=info.get("room_id"),
                    error_type=info.get("error_type", ""),
                    ip_index=slot.index,
                    now=time.monotonic(),
                )
                self._log(
                    "initial_connect_failed",
                    ip_index=slot.index, username=username,
                    room_id=st["room_id"], attempts=st["attempts"],
                    max_attempts=MAX_INITIAL_CONNECT_ATTEMPTS,
                    error_type=info.get("error_type"),
                    ip_specific=st["ip_specific"],
                    cooling_down=st["cooldown_until"] > time.monotonic(),
                )
                if st["cooldown_until"] > time.monotonic():
                    logger.warning(
                        "@%s: %d failed initial connection(s) for room %s -- not retrying "
                        "for %.0f min (each attempt costs a signature)",
                        username, st["attempts"], st["room_id"],
                        INITIAL_CONNECT_COOLDOWN_SEC / 60,
                    )

        return on_status

    async def _run_recording(self, slot: ProxySlot) -> None:
        username = slot.username
        settings = Settings.from_args(username, self.settings_kwargs["db_path"], self.settings_kwargs["idle_timeout"])
        settings = dataclasses.replace(settings, proxy_url=slot.proxy_url)
        runner = SessionRunner(self.conn, settings)
        # 乗り換え元から引き継ぐセッションがあれば、新規作成させず同じ行に
        # 書き戻させる。呼び出し側が room_id 一致を確認済みなので、client 側の
        # room_id 判定より優先する。
        runner.resume_session_id = slot.live_session_id
        slot.runner = runner
        try:
            await run_with_reconnect(
                runner, settings,
                on_status=self._make_on_status(slot),
                # 「一度も繋がらない」ときだけ効く上限。繋がった後の再接続には
                # 影響しない(録画実績のあるライブを軽々に手放さない)。
                max_initial_connect_failures=INITIAL_CONNECT_FAILURES_PER_DISPATCH,
            )
        except Exception:
            logger.exception("recording task crashed for @%s on ip#%d", username, slot.index)
        finally:
            # 乗り換え中(handing_off)は、このタスクが終わってもライブは
            # 続いている。ここで終了させると引き継ぎ先が新しいセッションを
            # 作ってしまう。
            #
            # manual_end() ではなく interrupted_end() を使う: この収集プロセスが
            # 録画をやめただけで、配信が終わったとは限らない。'manual' だと
            # find_resumable_session の再開対象から外れ、プロセスを再起動する
            # たびに同じライブが別セッションに割れる(2026-09-01 実例:
            # SIGINT 停止で5セッションが manual になり、再起動後すべて作り直された)。
            # 人が直接 Ctrl+C する watch.py / main.py は manual_end() のまま。
            if not runner.ended and not slot.handing_off:
                runner.interrupted_end()
            elif not slot.handing_off and (runner.live_session_id is not None
                                           or slot.live_session_id is not None):
                # 安全網: runner.ended が立っているのに live_session_id が
                # 残っている = 終了処理のあとで何かがセッションを開き直した
                # 不整合状態。放置すると status='live' のまま永久に残り、
                # ダッシュボードの「配信中」件数と進捗通知が実態とずれる
                # (2026-09-01 実例: session 46 が15分以上 live のまま。
                #  idle timeout を廃止したので、閉じ手が他にいない)。
                #
                # end_detection_type は 'interrupted' -- recover_stale_live_
                # sessions と同じく「決定的な終了シグナルを見ないまま録画が
                # 止まった」を意味する。'manual' にすると人が止めたことに
                # なって事実と違い、かつ再開対象から外れてしまう。
                # runner.live_session_id だけでは足りない。乗り換え先の runner が
                # 一度も接続できなかった場合(2026-09-01 実例: 署名の日次上限に
                # 当たって user_offline_exit で終了)、_ensure_session() が呼ばれず
                # runner はセッションを知らないまま終わる。一方で乗り換え元は
                # stop_for_handoff() でセッションを閉じずに手放しているので、
                # slot.live_session_id に残った側を見ないと誰も閉じない
                # (session 50 が live のまま143分放置された)。
                orphan = runner.live_session_id or slot.live_session_id
                still_open = self.conn.execute(
                    "SELECT 1 FROM live_sessions WHERE id = ? AND status = 'live'", (orphan,)
                ).fetchone()
                if still_open:
                    logger.warning(
                        "closing orphaned session id=%s for @%s (recording task ended without it)",
                        orphan, username,
                    )
                    db.end_session(self.conn, orphan, "interrupted")
                runner.live_session_id = None
            duration_sec = time.monotonic() - (slot.started_at or time.monotonic())
            self._log(
                "recording_ended",
                ip_index=slot.index,
                proxy=_masked(slot.proxy_url),
                username=username,
                duration_sec=round(duration_sec, 1),
            )
            slot.in_use = False
            slot.hard_stopping = False
            slot.username = None
            slot.task = None
            slot.runner = None
            slot.started_at = None
            if not slot.handing_off:
                slot.room_id = None
                slot.live_session_id = None
                slot.handoff_count = 0
                slot.last_handoff_at = 0.0
                slot.unknown_streak = 0
                slot.stall_reconnects = 0
                slot.stall_retries = 0
                slot.last_stall_reconnect_at = 0.0

    def _start_recording(self, slot: ProxySlot, username: str) -> None:
        slot.in_use = True
        slot.username = username
        slot.started_at = time.monotonic()
        active_count = sum(1 for s in self.slots if s.in_use)
        self.max_concurrent_seen = max(self.max_concurrent_seen, active_count)
        self._log(
            "recording_started",
            ip_index=slot.index,
            proxy=_masked(slot.proxy_url),
            username=username,
            active_count=active_count,
        )
        logger.info("@%s is live -- recording via ip#%d (active=%d)", username, slot.index, active_count)
        slot.task = asyncio.create_task(self._run_recording(slot))

    async def run_forever(self) -> None:
        while True:
            slot = self._next_recording_slot()
            username = self._next_candidate_username() if slot else None
            if slot is None or username is None:
                await asyncio.sleep(IDLE_WAIT_SEC)
                continue

            # 直前の初回接続失敗が IP固有の症状(InvalidStatusCode)だったなら、
            # 同じIPで叩き直さない。kyutyom は13回すべて同じIPから失敗した。
            avoid = self._connect_gate.avoid_ip_index(username)
            if avoid is not None and slot.index == avoid:
                alt = self._next_recording_slot()   # カーソルは進んでいるので別スロットになる
                if alt is not None and alt.index != avoid:
                    self._log("initial_connect_ip_switch", username=username,
                              from_ip_index=avoid, to_ip_index=alt.index)
                    slot = alt
                else:
                    # 空いているのが問題のIPだけ。次の周回に回す(署名は使わない)。
                    self._log("initial_connect_ip_switch_deferred",
                              username=username, avoid_ip_index=avoid)
                    await asyncio.sleep(IDLE_WAIT_SEC)
                    continue

            try:
                proxy_config = proxy_module.parse_proxy_url(slot.proxy_url)
            except ValueError as exc:
                self._log("invalid_proxy_url", ip_index=slot.index, error=str(exc))
                logger.error("ip#%d has an invalid proxy URL, dropping it from rotation: %s", slot.index, exc)
                self.slots.remove(slot)
                continue

            web_proxy = proxy_module.build_httpx_proxy(proxy_config)

            def _on_check_error(exc: Exception, _slot=slot, _username=username) -> None:
                # check_is_live() swallows every exception itself and always
                # returns False -- without this hook, a 403/block during the
                # is_live probe (the far more likely place to hit one, given
                # this trial's whole point is finding that threshold across
                # many distinct source IPs) would be indistinguishable from
                # "genuinely offline" and never show up in events.jsonl at all.
                self._log(
                    "check_error",
                    ip_index=_slot.index,
                    proxy=_masked(_slot.proxy_url),
                    username=_username,
                    error=repr(exc),
                )
                # 「存在しない/一度も配信していない」は再試行しても直らない。
                # 規定回数で巡回から外す(プロセス再起動でリセットされる)。
                if type(exc).__name__ == "UserNotFoundError":
                    self._not_found_counts[_username] += 1
                    if (self._not_found_counts[_username] >= QUARANTINE_AFTER_NOT_FOUND
                            and _username not in self._quarantined):
                        self._quarantined.add(_username)
                        logger.warning(
                            "quarantining @%s from the scan pool after %d UserNotFoundError(s) "
                            "-- restart to un-quarantine",
                            _username, self._not_found_counts[_username],
                        )
                        self._log("quarantined", username=_username,
                                  not_found_count=self._not_found_counts[_username],
                                  pool_remaining=len(self.pool) - len(self._quarantined))
                else:
                    # 別種のエラーなら連続とみなさずリセット(ネットワーク不調で
                    # 隔離してしまわないため)
                    self._not_found_counts.pop(_username, None)

            is_live = await self.pacer.check(username, web_proxy=web_proxy, on_error=_on_check_error)
            self._log(
                "check",
                ip_index=slot.index,
                proxy=_masked(slot.proxy_url),
                username=username,
                is_live=is_live,
            )
            if is_live:
                self._start_recording(slot, username)

    async def supervise_loop(self, interval_sec: float = SUPERVISE_INTERVAL_SEC) -> None:
        """録画中スロットを見張り、無イベントが続いたら別IPで生存を再確認する。

        設計の要点(2026-09-01):
          - 無イベントは「終了」ではなく「生存を再確認するトリガー」。以前は
            idle timeout でセッションを終了しており、それが1本の配信を複数
            セッションに割る原因だった。
          - 生存確認(is_live)は署名を消費しない。だから確認は積極的に行い、
            署名を消費する乗り換えだけをクールダウンで絞る。
          - 明示シグナル(LiveEndEvent / NORMAL_CLOSURE)を受けたセッションは
            client 側が即終了させるので、ここでは何もしない。
        """
        while True:
            await asyncio.sleep(interval_sec)
            try:
                await self._supervise_once()
            except Exception:
                logger.exception("supervise_loop iteration failed (continuing)")

    async def _supervise_once(self) -> None:
        now = time.monotonic()
        for slot in list(self.slots):
            if not slot.in_use or slot.runner is None or slot.handing_off:
                continue
            runner = slot.runner
            if runner.ended or runner.live_session_id is None:
                continue

            stalled = now - runner.last_event_at

            # --- 3段目: ハードタイムアウト ---
            # **一時停止(paused)の判定より前に置く。** 一時停止も結局は
            # 「イベントが来ない」状態であり、実測の停止は最長80秒だった。
            # 45分の停止と死んだ接続は外からは区別できないので、経過時間だけで
            # 判断するというこの段の原則をここでも通す。
            if stalled >= HARD_TIMEOUT_SEC and not slot.hard_stopping:
                await self._hard_timeout(slot, stalled)
                continue

            # 一時停止中は無イベントが正常(実測で80秒の停止あり)。確認しない。
            if getattr(runner, "paused", False):
                continue
            if stalled < STALL_THRESHOLD_SEC:
                continue

            # ここまで来たら「1分以上イベントが無い」。別IPで生存を確認する。
            slot.room_id = slot.room_id or runner.current_room_id()
            slot.live_session_id = runner.live_session_id
            await self._verify_or_handoff(slot, now)

    async def _hard_timeout(self, slot: ProxySlot, stalled: float) -> None:
        """最終イベントから HARD_TIMEOUT_SEC 過ぎたセッションを 'interrupted' で
        閉じ、スロットを解放する。生存確認も繋ぎ直しも行わないので署名は使わない。

        **ストール再接続(_verify_or_handoff の LONG_STALL 分岐)とは別物**。
        あちらは「繋ぎ直して録り続ける」ための処理で署名を消費する。こちらは
        「もう諦めてスロットを返す」ための処理で、何も消費しない。
        """
        slot.hard_stopping = True
        runner = slot.runner
        session_id = runner.live_session_id
        logger.error(
            "@%s: no events for %.0fs (hard timeout, threshold=%.0fs) -- closing session %s "
            "as 'interrupted' and releasing ip#%d",
            slot.username, stalled, HARD_TIMEOUT_SEC, session_id, slot.index,
        )
        self._log("hard_timeout", ip_index=slot.index, username=slot.username,
                  live_session_id=session_id, stalled_sec=round(stalled, 1),
                  threshold_sec=HARD_TIMEOUT_SEC,
                  stall_reconnects=slot.stall_reconnects)
        try:
            await runner.hard_stop("interrupted")
        except Exception:
            logger.exception("hard stop failed for @%s (continuing to release the slot)", slot.username)
        # disconnect() が効いても録画タスクが即座に終わるとは限らない。
        # スロットを確実に空けるのが目的なので、猶予後も生きていれば落とす。
        asyncio.create_task(self._cancel_task_if_stuck(slot, session_id))

    async def _cancel_task_if_stuck(self, slot: ProxySlot, session_id: int | None) -> None:
        task = slot.task
        await asyncio.sleep(HARD_STOP_GRACE_SEC)
        if task is not None and not task.done():
            logger.error(
                "recording task for session %s (@%s) did not exit %.0fs after the hard stop "
                "-- cancelling it to free ip#%d",
                session_id, slot.username, HARD_STOP_GRACE_SEC, slot.index,
            )
            self._log("hard_timeout_cancelled", ip_index=slot.index, username=slot.username,
                      live_session_id=session_id, grace_sec=HARD_STOP_GRACE_SEC)
            task.cancel()

    async def _verify_or_handoff(self, slot: ProxySlot, now: float) -> None:
        check_slot = self._borrow_check_slot()
        if check_slot is None:
            # 全IPが録画中。RESERVED_CHECK_SLOTS があるので通常は起きないが、
            # 起きたときは何もしない -- 録画中のIPを止めて確認に回すのは、
            # 確認のためにデータ取得を止めることになり本末転倒。
            self._log("stall_check_skipped", ip_index=slot.index,
                      username=slot.username, reason="no free ip")
            return

        username = slot.username
        try:
            proxy_config = proxy_module.parse_proxy_url(check_slot.proxy_url)
        except ValueError:
            return
        web_proxy = proxy_module.build_httpx_proxy(proxy_config)

        status = await self.pacer.check_status(username, web_proxy=web_proxy)
        self._log("stall_check", ip_index=slot.index, username=username,
                  check_via=_masked(check_slot.proxy_url), status=status,
                  is_live=(status == watch_module.LIVE),
                  unknown_streak=slot.unknown_streak,
                  stalled_sec=round(now - slot.runner.last_event_at, 1))

        if status == watch_module.UNKNOWN:
            # **確認できなかっただけ。終了の根拠にはならない。**
            # ネットワークやプロキシが一瞬詰まると UNKNOWN になる。これを
            # 「オフライン」と解釈すると録画中のセッションを誤って閉じる。
            # 次の周期に持ち越し、連続したときだけエラーとして報告する。
            slot.unknown_streak += 1
            if slot.unknown_streak >= MAX_UNKNOWN_STREAK:
                logger.warning(
                    "live check for @%s has been unavailable %d times in a row "
                    "(session %s kept open -- 'unknown' is not evidence the live ended)",
                    username, slot.unknown_streak, slot.live_session_id,
                )
                self._log("stall_check_unavailable", ip_index=slot.index, username=username,
                          live_session_id=slot.live_session_id,
                          unknown_streak=slot.unknown_streak)
            return

        slot.unknown_streak = 0   # 確定的な回答が得られたのでリセット

        if status == watch_module.OFFLINE:
            # ライブが本当に終わっていた。無イベントは終了のサインだった。
            logger.info("@%s confirmed offline via ip#%d -- ending session %s",
                        username, check_slot.index, slot.live_session_id)
            slot.runner.end_now("verified_offline")
            return

        # まだライブ中。
        stalled = now - slot.runner.last_event_at

        # --- 長時間ストールガード ---
        # 通常の無イベント(60秒程度)は待てば戻る(実測17/17)。自然回復の
        # 実測最大は834.5秒なので、その2倍(1680秒)を超えてなお届かないなら
        # 接続が本当に死んでいる可能性が高い。ここだけ
        # は1回繋ぎ直す。何度も繋ぎ直すと署名を浪費するうえ、繋ぎ直しても
        # 直らない相手(kana18724 型のWSハンドシェイク失敗)を延々と叩く
        # ことになるので、1セッション1回・リトライ2回・30分クールダウンで
        # 厳しく縛る。
        if stalled >= LONG_STALL_THRESHOLD_SEC:
            reason = None
            if slot.stall_reconnects >= MAX_STALL_RECONNECTS_PER_SESSION:
                reason = "already reconnected once for this session"
            elif slot.stall_retries >= MAX_STALL_RECONNECT_RETRIES:
                reason = "retry limit reached"
            elif now - slot.last_stall_reconnect_at < STALL_RECONNECT_COOLDOWN_SEC:
                reason = "cooldown"
            if reason:
                self._log("stall_reconnect_skipped", ip_index=slot.index, username=username,
                          live_session_id=slot.live_session_id, reason=reason,
                          stalled_sec=round(stalled, 1))
                return

            target = self._borrow_check_slot()
            if target is None or target is slot:
                self._log("stall_reconnect_skipped", ip_index=slot.index, username=username,
                          reason="no other ip available", stalled_sec=round(stalled, 1))
                return
            logger.warning(
                "@%s has been silent for %.0fs -- reconnecting once on a different IP "
                "(session %s, reconnect %d/%d)",
                username, stalled, slot.live_session_id,
                slot.stall_reconnects + 1, MAX_STALL_RECONNECTS_PER_SESSION,
            )
            self._log("stall_reconnect", ip_index=slot.index, to_ip_index=target.index,
                      username=username, live_session_id=slot.live_session_id,
                      stalled_sec=round(stalled, 1), attempt=slot.stall_reconnects + 1)
            slot.stall_retries += 1
            slot.last_stall_reconnect_at = now
            carried = slot.stall_reconnects + 1
            await self._handoff(slot, target, now)
            # 繋ぎ直し回数はセッションに紐づくので、引き継ぎ先へ持ち越す
            target.stall_reconnects = carried
            target.stall_retries = slot.stall_retries
            target.last_stall_reconnect_at = now
            return

        # 短い無イベントでは**何もしない**(接続はそのまま維持する)。
        # 実データ上、待てば戻るため、乗り換えは署名を使ってデータを
        # 途切れさせるだけだった -- HANDOFF_ENABLED のコメント参照。
        if not HANDOFF_ENABLED:
            self._log("stall_check_no_action", ip_index=slot.index, username=username,
                      live_session_id=slot.live_session_id,
                      reason="still live -- keeping the existing connection",
                      stalled_sec=round(stalled, 1))
            return

        if slot.handoff_count >= MAX_HANDOFFS_PER_SESSION:
            self._log("handoff_skipped", ip_index=slot.index, username=username,
                      reason="max handoffs reached", handoff_count=slot.handoff_count)
            return
        if now - slot.last_handoff_at < HANDOFF_COOLDOWN_SEC:
            self._log("handoff_skipped", ip_index=slot.index, username=username,
                      reason="cooldown",
                      wait_sec=round(HANDOFF_COOLDOWN_SEC - (now - slot.last_handoff_at), 1))
            return

        await self._handoff(slot, check_slot, now)

    async def _handoff(self, slot: ProxySlot, target: ProxySlot, now: float) -> None:
        """録画中のライブを別IPへ移す。セッションは閉じずに引き継ぐ。"""
        username = slot.username
        session_id = slot.live_session_id
        room_id = slot.room_id
        handoff_count = slot.handoff_count + 1

        self._log("handoff_started", ip_index=slot.index, to_ip_index=target.index,
                  username=username, live_session_id=session_id, room_id=room_id,
                  handoff_count=handoff_count)
        logger.info("handing off @%s from ip#%d to ip#%d (session=%s, handoff #%d)",
                    username, slot.index, target.index, session_id, handoff_count)

        # 乗り換え中の印。これが立っている間、元スロットの finally は
        # セッションを終了させない(引き継ぎ先が同じ行に書き続けるため)。
        slot.handing_off = True
        try:
            if slot.runner is not None:
                await slot.runner.stop_for_handoff()
            task = slot.task
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            slot.handing_off = False

        # 引き継ぎ先スロットに状態を移す
        target.room_id = room_id
        target.live_session_id = session_id
        target.handoff_count = handoff_count
        target.last_handoff_at = now
        self._start_recording(target, username)

    async def heartbeat_loop(self, interval_sec: float = HEARTBEAT_INTERVAL_SEC) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            active = sum(1 for s in self.slots if s.in_use)
            logger.info(
                "heartbeat: active=%d/%d ip(s) recording, max_concurrent_seen=%d, "
                "pool=%d(-%d quarantined), elapsed=%.0fs",
                active,
                len(self.slots),
                self.max_concurrent_seen,
                len(self.pool) - len(self._quarantined),
                len(self._quarantined),
                time.monotonic() - self._trial_started_at,
            )


async def _run_trial(trial: ProxyPoolTrial) -> None:
    await asyncio.gather(trial.run_forever(), trial.supervise_loop(), trial.heartbeat_loop())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-IP round-robin proxy trial: cycles a pool of proxy IPs checking is_live "
        "for a small test-account pool, one check per tick per DIFFERENT IP, recording whoever's "
        "found live through that same IP (1 IP = 1 concurrent recording)."
    )
    parser.add_argument(
        "--proxies-file",
        required=True,
        help="One proxy URL (http://user:pass@host:port) per line -- one distinct outbound IP each. "
        "Blank lines and #-comments ignored.",
    )
    parser.add_argument(
        "--pool-file",
        required=True,
        help="One TikTok unique_id per line -- the small set of known-active test streamers to check "
        "against. Blank lines and #-comments ignored.",
    )
    parser.add_argument(
        "--check-pace-sec",
        type=float,
        default=DEFAULT_CHECK_PACE_SEC,
        help=f"Seconds between successive check_is_live calls, regardless of which IP is used this "
        f"tick (default: {DEFAULT_CHECK_PACE_SEC:.1f} -- aggressive, for probing an untested IP "
        "pool's rate limit; use 5.0 to match this project's established safe rate once a limit is found).",
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help=f"Separate DB file (default: {DEFAULT_DB_PATH})")
    parser.add_argument(
        "--events-path",
        default=DEFAULT_EVENTS_PATH,
        help=f"jsonl log of every check/recording-start/recording-end/status event, each tagged with "
        f"which IP was involved (default: {DEFAULT_EVENTS_PATH})",
    )
    parser.add_argument("--idle-timeout", type=float, default=60.0, help="Seconds with no events before auto-ending a session (default: 60)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    proxy_urls = _read_proxy_urls_file(args.proxies_file)
    if not proxy_urls:
        logger.error("no proxy URLs found in %s -- add one http://user:pass@host:port per line", args.proxies_file)
        raise SystemExit(1)
    pool = _read_usernames_file(args.pool_file)
    if not pool:
        logger.error("no usernames found in %s", args.pool_file)
        raise SystemExit(1)

    logger.info(
        "loaded %d proxy IP(s), %d test username(s), check pace=%.1fs. Ctrl+C to stop.",
        len(proxy_urls),
        len(pool),
        args.check_pace_sec,
    )
    # 起動時に有効な閾値を1行で残す。定数を変えて再起動したのに反映されて
    # いない(古いプロセスが生きている)といった取り違えを、ログだけで
    # 判別できるようにするため。実測との突き合わせにも要る。
    logger.info(
        "guards: stall_check=%.0fs, stall_reconnect=%.0fs (max %d/session, cooldown %.0fs), "
        "hard_timeout=%.0fs, initial_connect=%d attempts/live (cooldown %.0fs), "
        "quarantine_after=%d not_found, handoff=%s",
        STALL_THRESHOLD_SEC,
        LONG_STALL_THRESHOLD_SEC, MAX_STALL_RECONNECTS_PER_SESSION, STALL_RECONNECT_COOLDOWN_SEC,
        HARD_TIMEOUT_SEC,
        MAX_INITIAL_CONNECT_ATTEMPTS, INITIAL_CONNECT_COOLDOWN_SEC,
        QUARANTINE_AFTER_NOT_FOUND,
        "on" if HANDOFF_ENABLED else "off",
    )

    conn = db.connect(args.db_path)
    db.init_schema(conn)
    stale_ids = db.recover_stale_live_sessions(conn)
    if stale_ids:
        logger.warning("recovered %d session(s) left 'live' by a previous run: %s", len(stale_ids), stale_ids)

    pacer = CheckPacer(pace_sec=args.check_pace_sec)
    trial = ProxyPoolTrial(
        conn,
        proxy_urls,
        pool,
        {"db_path": args.db_path, "idle_timeout": args.idle_timeout},
        pacer,
        args.events_path,
    )

    try:
        asyncio.run(_run_trial(trial))
    except KeyboardInterrupt:
        logger.info("keyboard interrupt received, shutting down. max_concurrent_seen=%d", trial.max_concurrent_seen)
    finally:
        for slot in trial.slots:
            if slot.runner is not None and not slot.runner.ended:
                slot.runner.interrupted_end()
        conn.close()


if __name__ == "__main__":
    main()
