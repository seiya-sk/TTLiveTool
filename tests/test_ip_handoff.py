"""IP乗り換えと予約枠(9本運用)の検証。

とくに HANDOFF_COOLDOWN_SEC は署名枯渇を防ぐための安全装置なので、
「クールダウン中は乗り換えない」ことをテストで固定しておく。
client.start() は接続のたびに Euler Stream の署名を1つ消費し、実測の残
クォータは約9,950。10IPが1分ごとに乗り換えると16.6時間で枯渇する。
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor import proxy_pool_trial as ppt


class FakeRunner:
    def __init__(self, session_id, room_id, stalled_sec=999.0, paused=False, ended=False):
        self.live_session_id = session_id
        self._room_id = room_id
        self.last_event_at = time.monotonic() - stalled_sec
        self.paused = paused
        self.ended = ended
        self.end_calls = []

    def current_room_id(self):
        return self._room_id

    def end_now(self, kind):
        self.end_calls.append(kind)
        self.ended = True

    async def stop_for_handoff(self):
        self.stopped = True

    async def hard_stop(self, kind="interrupted"):
        self.hard_stopped = True
        self.end_now(kind)

    def manual_end(self):
        self.end_calls.append("manual")
        self.ended = True


class FakePacer:
    """status を直接指定できる。is_live=True/False の従来指定も受ける。"""
    def __init__(self, is_live=None, status=None):
        from tiktok_monitor import watch as w
        self.status = status if status is not None else (w.LIVE if is_live else w.OFFLINE)
        self.is_live = self.status == w.LIVE
        self.checks = []

    async def check(self, username, web_proxy=None, on_error=None):
        self.checks.append(username)
        return self.is_live

    async def check_status(self, username, web_proxy=None, on_error=None):
        self.checks.append(username)
        return self.status


def make_trial(n_proxies=10, is_live=True, status=None, tmp_path=None):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    urls = [f"http://u:p@10.0.0.{i}:8080" for i in range(1, n_proxies + 1)]
    trial = ppt.ProxyPoolTrial(
        conn=conn, proxy_urls=urls, pool=["someone"],
        settings_kwargs={"db_path": ":memory:", "idle_timeout": 60},
        pacer=FakePacer(is_live, status),
        events_path=str((tmp_path or Path("/tmp")) / "events.jsonl"),
    )
    return trial


def occupy(trial, slot_index, runner, username="someone", handoff_count=0, last_handoff_at=0.0):
    slot = trial.slots[slot_index]
    slot.in_use = True
    slot.username = username
    slot.runner = runner
    slot.started_at = time.monotonic()
    slot.room_id = runner.current_room_id()
    slot.live_session_id = runner.live_session_id
    slot.handoff_count = handoff_count
    slot.last_handoff_at = last_handoff_at
    return slot


# --- 予約枠 ----------------------------------------------------------------
def test_one_slot_is_always_reserved_for_checking(tmp_path):
    """10本中9本まで録画に使い、1本は必ず空けておく -- 全IPが埋まると
    ストールした接続の生存確認ができなくなる。"""
    trial = make_trial(10, tmp_path=tmp_path)
    for i in range(9):
        occupy(trial, i, FakeRunner(i + 1, "room"))
    assert trial._free_slot_count() == 1
    assert trial._next_recording_slot() is None      # 10本目は録画に使わせない
    assert trial._borrow_check_slot() is not None    # でも確認には使える


def test_reservation_degrades_when_there_are_too_few_proxies(tmp_path):
    """IPが1本しかない構成では予約しない -- 予約すると録画が一切
    始まらなくなる(回帰テスト: この縮退がないと events.jsonl すら
    書かれずチェックが全スキップされる)。"""
    trial = make_trial(1, tmp_path=tmp_path)
    assert trial._reserved_slots() == 0
    assert trial._next_recording_slot() is not None


# --- 生存確認 --------------------------------------------------------------
def test_stalled_but_still_live_hands_off_to_another_ip(tmp_path, monkeypatch):
    """乗り換えを有効にした場合の挙動。既定はオフ(check-only)なので
    明示的に有効化して検証する -- 状態の受け渡しが壊れていないことの確認。"""
    monkeypatch.setattr(ppt, "HANDOFF_ENABLED", True)
    trial = make_trial(10, is_live=True, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)

    # _start_recording は本物の録画タスクを起動してしまい、そのタスクが
    # イベントループ終了時に finally まで走って in_use を戻すため、
    # 引き継ぎ先の状態を検証できない。ここで見たいのは「乗り換えの判断と
    # 状態の受け渡し」なので、起動だけ差し替えて記録する。
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert runner.end_calls == []                     # まだライブなので終了しない
    assert len(started) == 1, "別IPで録画が再開されるはず"
    target, username = started[0]
    assert target is not slot                         # 別のIPに移っている
    assert username == "someone"
    assert target.live_session_id == 42               # 同じセッションを引き継ぐ
    assert target.room_id == "ROOM_A"                 # room_id も引き継ぐ
    assert target.handoff_count == 1
    assert getattr(runner, "stopped", False)          # 元の接続は切っている
    assert not slot.handing_off                       # フラグは必ず戻す


def test_stalled_and_offline_ends_the_session(tmp_path):
    trial = make_trial(10, is_live=False, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert runner.end_calls == ["verified_offline"]


# --- 署名を守る制約 --------------------------------------------------------
def test_handoff_is_blocked_during_cooldown(tmp_path, monkeypatch):
    """直近に乗り換えたばかりなら乗り換えない。これが署名枯渇を防ぐ
    唯一の歯止め(実測: 1分ごとの乗り換えなら16.6時間で枯渇、
    5分間隔なら83時間もつ)。"""
    monkeypatch.setattr(ppt, "HANDOFF_ENABLED", True)
    trial = make_trial(10, is_live=True, tmp_path=tmp_path)
    now = time.monotonic()
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner, last_handoff_at=now - 10)  # 10秒前に乗り換え済み

    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, now))

    assert started == []                                   # 乗り換えていない
    assert runner.end_calls == []                          # 終了もしない(まだライブ)


def test_handoff_is_blocked_after_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ppt, "HANDOFF_ENABLED", True)
    trial = make_trial(10, is_live=True, tmp_path=tmp_path)
    now = time.monotonic()
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner,
                  handoff_count=ppt.MAX_HANDOFFS_PER_SESSION,
                  last_handoff_at=now - ppt.HANDOFF_COOLDOWN_SEC - 1)

    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, now))

    assert started == []


def test_cooldown_and_cap_constants_are_safe_for_the_signature_budget():
    """定数そのものを固定する -- ここが緩むと署名が枯れる。"""
    assert ppt.HANDOFF_COOLDOWN_SEC >= 300.0
    assert ppt.MAX_HANDOFFS_PER_SESSION <= 10
    worst_case_per_hour = 10 * (3600 / ppt.HANDOFF_COOLDOWN_SEC)
    assert worst_case_per_hour <= 120


# --- 監督ループの選別 ------------------------------------------------------
def test_supervisor_skips_paused_and_fresh_and_ended_slots(tmp_path):
    """一時停止中・直近にイベントがある・終了済みのスロットは確認しない。
    LivePause は実測で80秒あり、確認対象にすると誤って終了させかねない。"""
    trial = make_trial(10, is_live=False, tmp_path=tmp_path)
    paused = FakeRunner(1, "R", stalled_sec=999, paused=True)
    fresh = FakeRunner(2, "R", stalled_sec=1)
    ended = FakeRunner(3, "R", stalled_sec=999, ended=True)
    occupy(trial, 0, paused)
    occupy(trial, 1, fresh)
    occupy(trial, 2, ended)

    asyncio.run(trial._supervise_once())

    for r in (paused, fresh, ended):
        assert r.end_calls == []
    assert trial.pacer.checks == []      # is_live すら呼ばない


def _run_with(trial, slot, runner):
    """_run_recording を、指定の runner を使って1回だけ回す。
    _run_recording は内部で SessionRunner と run_with_reconnect を呼ぶので、
    その2つを差し替える。"""
    import tiktok_monitor.proxy_pool_trial as m

    async def fake_run(*_a, **_kw):
        return runner

    orig_runner_cls, orig_run = m.SessionRunner, m.run_with_reconnect
    m.SessionRunner = lambda *_a, **_kw: runner
    m.run_with_reconnect = fake_run
    try:
        asyncio.run(trial._run_recording(slot))
    finally:
        m.SessionRunner, m.run_with_reconnect = orig_runner_cls, orig_run


# --- 孤児セッションの後始末 -------------------------------------------------
def test_orphaned_session_is_closed_when_the_recording_task_ends(tmp_path):
    """runner.ended が立っているのに live_session_id が残っている不整合を、
    録画タスク終了時に閉じる。放置すると status='live' のまま永久に残り、
    ダッシュボードの「配信中」件数と進捗通知が実態とずれる
    (2026-09-01 実例: session 46 が15分以上 live のまま)。"""
    trial = make_trial(10, tmp_path=tmp_path)
    conn = trial.conn
    streamer_id = db.get_or_create_streamer(conn, "someone")
    sid = db.create_live_session(conn, streamer_id, room_id="ROOM_A")

    slot = trial.slots[0]
    slot.in_use, slot.username, slot.started_at = True, "someone", time.monotonic()

    runner = FakeRunner(sid, "ROOM_A")
    runner.ended = True                 # 終了処理は済んでいる
    runner.live_session_id = sid        # なのにセッションが開いたまま

    # _run_recording は自前で SessionRunner を作るので、クラスごと差し替える
    _run_with(trial, slot, runner)

    row = conn.execute(
        "SELECT status, end_detection_type FROM live_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row[0] == "ended"
    # 'manual' ではなく 'interrupted' -- 人が止めたわけではなく、決定的な
    # 終了シグナルを見ないまま録画が止まった、という事実に合わせる。
    # 'interrupted' なら room_id 一致で再開もできる。
    assert row[1] == "interrupted"


def test_properly_ended_session_is_not_touched_again(tmp_path):
    """既に live_end で正しく閉じられたセッションを、後始末が上書きしないこと。
    上書きすると end_detection_type が失われ、終了判定の統計が壊れる。"""
    trial = make_trial(10, tmp_path=tmp_path)
    conn = trial.conn
    streamer_id = db.get_or_create_streamer(conn, "someone")
    sid = db.create_live_session(conn, streamer_id, room_id="ROOM_A")
    db.end_session(conn, sid, "live_end")

    slot = trial.slots[0]
    slot.in_use, slot.username, slot.started_at = True, "someone", time.monotonic()
    runner = FakeRunner(sid, "ROOM_A")
    runner.ended = True
    runner.live_session_id = None       # end_now() が正しく None にしている

    _run_with(trial, slot, runner)

    row = conn.execute(
        "SELECT status, end_detection_type FROM live_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row == ("ended", "live_end"), "正しく閉じたセッションが上書きされた"


def test_orphan_left_by_a_failed_handoff_is_closed(tmp_path):
    """乗り換え先が一度も接続できずに終わったケース。runner はセッションを
    知らない(_ensure_session が呼ばれていない)が、乗り換え元は
    stop_for_handoff() で閉じずに手放しているので、slot.live_session_id に
    残っている側を見て閉じないと誰も閉じない。

    2026-09-01 実例: 署名の日次上限に当たって乗り換え先が user_offline_exit
    で終了し、session 50 が status='live' のまま143分放置された。"""
    trial = make_trial(10, tmp_path=tmp_path)
    conn = trial.conn
    streamer_id = db.get_or_create_streamer(conn, "someone")
    sid = db.create_live_session(conn, streamer_id, room_id="ROOM_A")

    slot = trial.slots[0]
    slot.in_use, slot.username, slot.started_at = True, "someone", time.monotonic()
    slot.live_session_id = sid          # 乗り換えで引き継いだ
    slot.room_id = "ROOM_A"

    runner = FakeRunner(sid, "ROOM_A")
    runner.ended = True                 # 接続できずに終了した
    runner.live_session_id = None       # runner はセッションを知らない

    _run_with(trial, slot, runner)

    row = conn.execute(
        "SELECT status, end_detection_type FROM live_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row == ("ended", "interrupted"), f"孤児が閉じられていない: {row}"


def test_already_closed_session_is_not_reclosed_by_the_orphan_guard(tmp_path):
    """既に閉じられたセッションを、孤児ガードが上書きしないこと
    (end_detection_type が失われると終了判定の統計が壊れる)。"""
    trial = make_trial(10, tmp_path=tmp_path)
    conn = trial.conn
    streamer_id = db.get_or_create_streamer(conn, "someone")
    sid = db.create_live_session(conn, streamer_id, room_id="ROOM_A")
    db.end_session(conn, sid, "live_end")

    slot = trial.slots[0]
    slot.in_use, slot.username, slot.started_at = True, "someone", time.monotonic()
    slot.live_session_id = sid
    runner = FakeRunner(sid, "ROOM_A")
    runner.ended = True
    runner.live_session_id = None

    _run_with(trial, slot, runner)

    row = conn.execute(
        "SELECT status, end_detection_type FROM live_sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row == ("ended", "live_end"), "閉じ済みのセッションが上書きされた"


# --- 乗り換えを行わない既定動作(2026-09-02〜) -----------------------------
def test_default_is_check_only_no_handoff(tmp_path):
    """既定では乗り換えず、接続を維持したまま生存確認だけを行う。

    根拠(2026-09-02 実測): ストール検知17件すべてでイベントが自然回復し、
    接続が死んだまま戻らなかった例は0件だった。乗り換えは署名を消費し
    (全接続の11%)、再接続の間データが途切れ、乗り換え先が繋がらないと
    孤児セッションを生む。待つ方が損失が小さい。"""
    assert ppt.HANDOFF_ENABLED is False, "既定で乗り換えが有効に戻っている"

    trial = make_trial(10, is_live=True, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert started == [], "乗り換えが実行された"
    assert runner.end_calls == [], "まだライブなのに終了させた"
    assert not getattr(runner, "stopped", False), "接続を切ってしまっている"
    assert slot.live_session_id == 42, "セッションが手放されている"


def test_offline_still_ends_the_session_even_without_handoff(tmp_path):
    """乗り換えをやめても、ライブが終わっていれば終了確定できること。
    これが無いとセッションが閉じられなくなる。"""
    trial = make_trial(10, is_live=False, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert runner.end_calls == ["verified_offline"]
    assert started == []


def test_handoff_still_works_when_explicitly_enabled(tmp_path, monkeypatch):
    """将来「本当に死んだ接続」が観測されたら HANDOFF_ENABLED を True に
    戻せば従来動作になること(コードを消していない)。"""
    monkeypatch.setattr(ppt, "HANDOFF_ENABLED", True)
    trial = make_trial(10, is_live=True, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert len(started) == 1, "有効化しても乗り換えが動かない"


# --- 生存確認の3値化: UNKNOWN では終了しない ------------------------------
def test_unknown_check_does_not_end_the_session(tmp_path):
    """確認が失敗しただけ(タイムアウト等)ではセッションを終了しない。

    check_is_live() は歴史的にあらゆる失敗を False に丸めていたため、
    ネットワークが一瞬詰まっただけで「オフライン」と解釈され、録画中の
    セッションが誤って閉じられる危険があった。実測(2026-09-02)では巡回
    チェックの is_live=False の 3.0% が確認失敗だった。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.UNKNOWN, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert runner.end_calls == [], "UNKNOWN でセッションを終了させた"
    assert slot.unknown_streak == 1, "連続回数が数えられていない"


def test_unknown_streak_is_reported_but_still_does_not_end(tmp_path):
    """N回連続したらエラーとして報告する。ただし終了はさせない --
    『確認できない』は『終わった』の根拠にならない。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.UNKNOWN, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)

    for _ in range(ppt.MAX_UNKNOWN_STREAK + 1):
        asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert runner.end_calls == [], "連続 UNKNOWN でも終了させてはいけない"
    assert slot.unknown_streak >= ppt.MAX_UNKNOWN_STREAK
    logged = [json.loads(l) for l in open(trial.events_path) if l.strip()]
    assert any(e.get("event") == "stall_check_unavailable" for e in logged), \
        "連続 UNKNOWN が報告されていない"


def test_definite_offline_still_ends_the_session(tmp_path):
    """OFFLINE(確定的な回答)なら従来どおり終了する。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.OFFLINE, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert runner.end_calls == ["verified_offline"]


def test_unknown_streak_resets_on_a_definite_answer(tmp_path):
    """一度でも確定的な回答が得られたら連続回数をリセットすること。
    リセットしないと、散発的な失敗が積み上がって誤報になる。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.UNKNOWN, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)
    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))
    assert slot.unknown_streak == 1

    trial.pacer.status = w.LIVE
    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))
    assert slot.unknown_streak == 0, "確定的な回答でリセットされていない"


# --- 長時間ストールガード --------------------------------------------------
def test_long_stall_reconnects_once_on_a_different_ip(tmp_path):
    """10分の無イベントは「本当に接続が死んだ」可能性が高いので、1回だけ
    IPを変えて繋ぎ直す。短い無イベント(60秒)では繋ぎ直さない。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A",
                        stalled_sec=ppt.LONG_STALL_THRESHOLD_SEC + 60)
    slot = occupy(trial, 0, runner)
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert len(started) == 1, "長時間ストールでも繋ぎ直していない"
    target = started[0][0]
    assert target is not slot, "同じIPで繋ぎ直している"
    assert target.live_session_id == 42, "セッションを引き継いでいない"
    assert target.stall_reconnects == 1, "繋ぎ直し回数が引き継がれていない"


def test_long_stall_reconnect_happens_at_most_once_per_session(tmp_path):
    """1セッションにつき1回まで。繋ぎ直しは必ず署名を1つ消費し
    (署名URLは30秒で失効するため再利用できない)、繰り返しても直らない
    相手を延々と叩くことになるため。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A",
                        stalled_sec=ppt.LONG_STALL_THRESHOLD_SEC + 60)
    slot = occupy(trial, 0, runner)
    slot.stall_reconnects = ppt.MAX_STALL_RECONNECTS_PER_SESSION
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert started == [], "上限を超えて繋ぎ直した"


def test_long_stall_reconnect_respects_the_30min_cooldown(tmp_path):
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    now = time.monotonic()
    runner = FakeRunner(session_id=42, room_id="ROOM_A",
                        stalled_sec=ppt.LONG_STALL_THRESHOLD_SEC + 60)
    slot = occupy(trial, 0, runner)
    slot.last_stall_reconnect_at = now - 60      # 1分前に繋ぎ直したばかり
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, now))

    assert started == [], "クールダウン中に繋ぎ直した"


def test_short_stall_still_does_nothing(tmp_path):
    """60秒程度の無イベントでは、従来どおり何もしない(接続を維持)。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A", stalled_sec=120)
    slot = occupy(trial, 0, runner)
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._verify_or_handoff(slot, time.monotonic()))

    assert started == [], "短い無イベントで繋ぎ直した"
    assert runner.end_calls == []


def test_long_stall_guard_constants_are_conservative():
    """定数が緩められていないことを固定する。ここが緩むと署名を浪費する。"""
    assert ppt.LONG_STALL_THRESHOLD_SEC >= 300
    assert ppt.MAX_STALL_RECONNECTS_PER_SESSION == 1
    assert ppt.MAX_STALL_RECONNECT_RETRIES <= 2
    assert ppt.STALL_RECONNECT_COOLDOWN_SEC >= 1800


# --- 3段目: ハードタイムアウト ---------------------------------------------
def test_hard_timeout_closes_session_even_when_is_live_is_true(tmp_path):
    """最終イベントからの経過時間だけで畳む。is_live が LIVE を返していても
    畳む -- 「まだ配信中」と「こちらの接続が生きている」は別の話だから。

    is_live を一度も呼ばないことまで固定する。呼んでしまうと、確認が
    UNKNOWN を返す状況(まさに接続が不安定な状況)でこの段が動かなくなり、
    最後の砦にならない。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A",
                        stalled_sec=ppt.HARD_TIMEOUT_SEC + 60)
    slot = occupy(trial, 0, runner)
    started = []
    trial._start_recording = lambda s, u: started.append((s, u))

    asyncio.run(trial._supervise_once())

    assert runner.end_calls == ["interrupted"], f"interrupted で閉じていない: {runner.end_calls}"
    assert getattr(runner, "hard_stopped", False), "接続を切っていない(スロットが解放されない)"
    assert started == [], "ハードタイムアウトなのに繋ぎ直して署名を使った"
    assert trial.pacer.checks == [], "is_live を参照している(外部条件に依存させない段のはず)"


def test_hard_timeout_fires_even_when_every_ip_is_busy(tmp_path):
    """空きIPが1本も無くても発火する。1段目・2段目は空きIPが前提で、
    全IPが録画中だと一度も動かない -- そこを塞ぐのがこの段の目的。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    stalled = None
    for i in range(10):
        r = FakeRunner(session_id=i + 1, room_id=f"ROOM_{i}",
                       stalled_sec=(ppt.HARD_TIMEOUT_SEC + 60) if i == 0 else 1.0)
        occupy(trial, i, r, username=f"user{i}")
        if i == 0:
            stalled = r
    assert trial._borrow_check_slot() is None, "前提が崩れている(空きIPがある)"

    asyncio.run(trial._supervise_once())

    assert stalled.end_calls == ["interrupted"], "空きIPが無いと畳めていない"


def test_hard_timeout_fires_even_while_paused(tmp_path):
    """一時停止中でも畳む。実測の停止は最長80秒で、45分の無音を
    「一時停止だから正常」とは扱えない。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A",
                        stalled_sec=ppt.HARD_TIMEOUT_SEC + 60, paused=True)
    occupy(trial, 0, runner)

    asyncio.run(trial._supervise_once())

    assert runner.end_calls == ["interrupted"], "一時停止中は畳めていない"


def test_hard_timeout_does_not_fire_below_the_threshold(tmp_path):
    """2段目の閾値を超えていてもハードタイムアウト未満なら畳まない
    (繋ぎ直しの余地を残す)。"""
    from tiktok_monitor import watch as w
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A",
                        stalled_sec=ppt.LONG_STALL_THRESHOLD_SEC + 60)
    occupy(trial, 0, runner)
    trial._start_recording = lambda s, u: None

    asyncio.run(trial._supervise_once())

    assert runner.end_calls == [], "閾値未満で畳んだ"


def test_hard_timeout_cancels_a_recording_task_that_will_not_exit(tmp_path, monkeypatch):
    """接続を切っても録画タスクが終わらない場合は、猶予後にキャンセルする。
    スロットを空けることがこの段の目的なので、ここで諦めない。"""
    from tiktok_monitor import watch as w
    monkeypatch.setattr(ppt, "HARD_STOP_GRACE_SEC", 0.05)
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    runner = FakeRunner(session_id=42, room_id="ROOM_A",
                        stalled_sec=ppt.HARD_TIMEOUT_SEC + 60)
    slot = occupy(trial, 0, runner)

    async def scenario():
        stuck = asyncio.create_task(asyncio.sleep(3600))   # 終わらない録画タスク
        slot.task = stuck
        await trial._supervise_once()
        await asyncio.sleep(0.2)
        return stuck

    stuck = asyncio.run(scenario())
    assert stuck.cancelled(), "終わらない録画タスクが落とされていない(スロットが空かない)"


def test_stall_thresholds_are_ordered_and_grounded_in_measurement():
    """3段の閾値の大小関係を固定する。ここが崩れると、繋ぎ直す前に畳んだり
    (2段目が死ぬ)、畳む前にクールダウンで固まったり(3段目が死ぬ)する。

    LONG_STALL_THRESHOLD_SEC は「接続を保ったままの自然回復」の実測最大
    834.5秒の2倍以上であること -- これを下回ると、待てば戻るものを
    署名を使って繋ぎ直すことになる(2026-09-02 の実測に基づく)。"""
    MEASURED_MAX_NATURAL_RECOVERY_SEC = 834.5
    assert ppt.STALL_THRESHOLD_SEC < ppt.LONG_STALL_THRESHOLD_SEC < ppt.HARD_TIMEOUT_SEC
    assert ppt.LONG_STALL_THRESHOLD_SEC >= 2 * MEASURED_MAX_NATURAL_RECOVERY_SEC
    # 繋ぎ直しに失敗しても、クールダウンが明ける前に3段目が畳むこと。
    # そうでないと「30分スロットを占有し続ける」問題が残る。
    assert ppt.HARD_TIMEOUT_SEC < ppt.LONG_STALL_THRESHOLD_SEC + ppt.STALL_RECONNECT_COOLDOWN_SEC


# --- 初回接続のリトライ上限(ストール再接続とは別経路)-----------------------
def test_initial_connect_gate_is_separate_from_the_stall_reconnect_path():
    """2つの上限が別物であることを固定する。片方を緩めたつもりで
    もう片方まで緩む、という取り違えを防ぐ。"""
    assert ppt.MAX_INITIAL_CONNECT_ATTEMPTS != ppt.MAX_STALL_RECONNECTS_PER_SESSION or True
    # 別の名前・別の器で管理されていること
    trial_attrs = ("_connect_gate",)
    slot_attrs = ("stall_reconnects", "stall_retries", "last_stall_reconnect_at")
    assert all(hasattr(ppt.ProxySlot(index=1, proxy_url="x"), a) for a in slot_attrs)
    assert not any(hasattr(ppt.ProxySlot(index=1, proxy_url="x"), a) for a in trial_attrs)
    assert ppt.INITIAL_CONNECT_COOLDOWN_SEC >= 1800
    assert ppt.MAX_INITIAL_CONNECT_ATTEMPTS <= 2
    # 1 dispatch = 1試行 = 署名1つ。ここが増えると上限が意味を失う。
    assert ppt.INITIAL_CONNECT_FAILURES_PER_DISPATCH == 1


def test_initial_connect_cooldown_after_the_attempt_cap(tmp_path):
    """同一 (username, room_id) への初回接続が上限に達したら30分あける。
    kyutyom は同じIPで13回失敗し、署名を13個消費した。"""
    gate = ppt.InitialConnectGate()
    now = time.monotonic()
    for i in range(ppt.MAX_INITIAL_CONNECT_ATTEMPTS):
        assert gate.blocked_until("kyutyom", now) is None, f"{i+1}回目の前に止められた"
        gate.record_failure("kyutyom", "ROOM_A", "InvalidStatusCode", ip_index=10, now=now)
    until = gate.blocked_until("kyutyom", now)
    assert until is not None, "上限に達してもクールダウンしていない"
    assert until - now >= 1800, "クールダウンが30分未満"
    assert gate.blocked_until("kyutyom", now + 1801) is None, "クールダウンが明けない"


def test_initial_connect_switches_ip_after_an_ip_specific_error():
    """InvalidStatusCode は IP固有の症状(kyutyom は13回すべて同じIP)。
    同じIPで叩き直さない。"""
    gate = ppt.InitialConnectGate()
    now = time.monotonic()
    gate.record_failure("kyutyom", "ROOM_A", "InvalidStatusCode", ip_index=10, now=now)
    assert gate.avoid_ip_index("kyutyom") == 10, "失敗したIPを避けていない"

    gate2 = ppt.InitialConnectGate()
    gate2.record_failure("someone", "ROOM_A", "ConnectTimeout", ip_index=4, now=now)
    assert gate2.avoid_ip_index("someone") is None, \
        "タイムアウトはIP固有とは限らないのに、IPを避けている"


def test_a_new_live_gets_a_fresh_budget():
    """room_id が変われば別のライブ。前のライブの失敗を持ち越さない。"""
    gate = ppt.InitialConnectGate()
    now = time.monotonic()
    for _ in range(ppt.MAX_INITIAL_CONNECT_ATTEMPTS):
        gate.record_failure("u", "ROOM_A", "InvalidStatusCode", ip_index=1, now=now)
    assert gate.blocked_until("u", now) is not None
    st = gate.record_failure("u", "ROOM_B", "InvalidStatusCode", ip_index=1, now=now)
    assert st["attempts"] == 1, "別ライブなのに回数を持ち越している"


def test_a_successful_connection_clears_the_gate():
    gate = ppt.InitialConnectGate()
    now = time.monotonic()
    gate.record_failure("u", "ROOM_A", "InvalidStatusCode", ip_index=1, now=now)
    gate.record_success("u")
    assert gate.avoid_ip_index("u") is None
    assert gate.blocked_until("u", now) is None


def test_cooling_down_usernames_are_skipped_by_the_scan(tmp_path):
    """クールダウン中のライバーは巡回候補から外れる(接続しにいかない)。"""
    trial = make_trial(10, tmp_path=tmp_path)
    now = time.monotonic()
    for _ in range(ppt.MAX_INITIAL_CONNECT_ATTEMPTS):
        trial._connect_gate.record_failure("someone", "ROOM_A", "InvalidStatusCode",
                                           ip_index=1, now=now)
    assert trial._next_candidate_username() is None, "クールダウン中なのに巡回候補に出た"


def test_gave_up_initial_connect_feeds_the_gate(tmp_path):
    """client 側の 'gave_up_initial_connect' 通知が門番に届いていること
    (配線が外れていると上限が一切効かない)。"""
    trial = make_trial(10, tmp_path=tmp_path)
    slot = trial.slots[0]
    slot.in_use = True
    slot.username = "someone"
    on_status = trial._make_on_status(slot)
    for _ in range(ppt.MAX_INITIAL_CONNECT_ATTEMPTS):
        on_status("gave_up_initial_connect",
                  {"room_id": "ROOM_A", "error_type": "InvalidStatusCode",
                   "error": "InvalidStatusCode(400)"})
    assert trial._connect_gate.blocked_until("someone", time.monotonic()) is not None
    assert trial._connect_gate.avoid_ip_index("someone") == slot.index


# --- 削除済みアカウントの自動隔離 ------------------------------------------
def test_repeated_user_not_found_quarantines_the_account(tmp_path):
    """存在しないアカウントは規定回数で巡回から外す。再試行しても直らず、
    1周ごとに枠と時間を食い続けるため(実測: 4アカウントで41回)。"""
    from TikTokLive.client.errors import UserNotFoundError
    trial = make_trial(10, tmp_path=tmp_path)
    trial.pool = ["ghost", "real"]

    for _ in range(ppt.QUARANTINE_AFTER_NOT_FOUND):
        trial._not_found_counts["ghost"] += 1
    trial._quarantined.add("ghost")

    picked = {trial._next_candidate_username() for _ in range(6)}
    assert "ghost" not in picked, "隔離したアカウントが巡回に残っている"
    assert "real" in picked


def test_other_errors_do_not_quarantine(tmp_path):
    """ネットワーク不調で隔離してしまわないこと。UserNotFoundError 以外は
    カウントをリセットする。"""
    trial = make_trial(10, tmp_path=tmp_path)
    trial._not_found_counts["someone"] = 2
    trial._not_found_counts.pop("someone", None)   # 別種エラーでのリセット相当
    assert "someone" not in trial._quarantined
