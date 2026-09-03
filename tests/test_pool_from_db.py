"""監視対象をDBから読む(UIの無効化が録画に効くこと)。

以前は起動時に --pool-file を1回読むだけで、UIで「無効にする」を押しても
録画は止まらなかった。画面の表示と実際の動作が食い違っている状態で、
今日直した他の不具合(昇順で金メダル、判定不能なのに0件表示)と同じ性質。

**読み直しの失敗で全停止しないこと**が要点。DBロックで監視対象が空になると
録画がすべて止まる。2026-09-02 の一晩で database is locked を24件観測して
おり、現実的なリスクとして扱う。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tiktok_monitor import db
from tiktok_monitor import proxy_pool_trial as ppt
from tests.test_ip_handoff import make_trial


def seed(conn, names):
    for n in names:
        db.get_or_create_streamer(conn, n)
    conn.commit()


def test_only_enabled_and_unarchived_are_monitored():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    seed(conn, ["active", "disabled", "archived"])
    db.set_streamer_enabled(conn, db.get_or_create_streamer(conn, "disabled"), False)
    db.archive_streamer(conn, db.get_or_create_streamer(conn, "archived"))

    assert db.list_monitored_usernames(conn) == ["active"]


def test_pool_is_refreshed_after_a_full_cycle(tmp_path):
    """UIで無効にした内容が次の巡回から効くこと。"""
    trial = make_trial(10, tmp_path=tmp_path)
    trial.pool = ["a", "b"]
    current = {"list": ["a"]}          # bが無効化された想定
    trial.pool_loader = lambda: list(current["list"])
    trial._last_pool_refresh = 0.0
    trial._pool_cursor = 0
    trial._pool_cycle_start = 0

    trial._maybe_refresh_pool()

    assert trial.pool == ["a"], "読み直しが反映されていない"


def test_refresh_failure_keeps_the_previous_pool(tmp_path, caplog):
    """**DBロックで監視対象が空になり全停止する事故を避ける。**"""
    trial = make_trial(10, tmp_path=tmp_path)
    trial.pool = ["a", "b", "c"]
    def boom():
        raise RuntimeError("database is locked")
    trial.pool_loader = boom
    trial._last_pool_refresh = 0.0

    trial._maybe_refresh_pool()      # 例外を投げないこと

    assert trial.pool == ["a", "b", "c"], "失敗時に監視対象が失われた"


def test_empty_refresh_keeps_the_previous_pool_and_warns(tmp_path, caplog):
    """0件でも停止しない。全員アーカイブのような意図的な操作もありうるが、
    その判断を巡回ループに持たせるより、動き続けて人が気づくほうが安全。"""
    trial = make_trial(10, tmp_path=tmp_path)
    trial.pool = ["a", "b"]
    trial.pool_loader = lambda: []
    trial._last_pool_refresh = 0.0

    with caplog.at_level("WARNING"):
        trial._maybe_refresh_pool()

    assert trial.pool == ["a", "b"], "0件で監視対象が空になった"
    assert any("0人" in r.message for r in caplog.records), "警告が出ていない"


def test_refresh_is_rate_limited(tmp_path):
    """毎回DBを叩かない。巡回1周は最短でも数分かかるが、
    プールが極端に小さいと1周が一瞬で終わるため下限を置く。"""
    trial = make_trial(10, tmp_path=tmp_path)
    trial.pool = ["a"]
    calls = {"n": 0}
    def loader():
        calls["n"] += 1
        return ["a"]
    trial.pool_loader = loader
    trial._last_pool_refresh = 0.0
    trial._maybe_refresh_pool()
    trial._maybe_refresh_pool()       # 直後の2回目は読まない
    assert calls["n"] == 1, f"読み直しが抑制されていない({calls['n']}回)"


def test_no_loader_means_no_refresh(tmp_path):
    """--pool-file 指定時は固定リスト。勝手にDBへ切り替わらないこと。"""
    trial = make_trial(10, tmp_path=tmp_path)
    trial.pool = ["a", "b"]
    trial.pool_loader = None
    trial._maybe_refresh_pool()
    assert trial.pool == ["a", "b"]


def test_disabling_does_not_stop_an_in_flight_recording(tmp_path):
    """録画中のライバーを無効化しても、そのライブは完走させる。
    途中で切ると不完全なデータが残る。無効化は「次から巡回しない」であって
    「今すぐ止める」ではない。"""
    from tests.test_ip_handoff import FakeRunner, occupy
    trial = make_trial(10, tmp_path=tmp_path)
    trial.pool = ["recording_now", "other"]
    runner = FakeRunner(session_id=1, room_id="ROOM_A", stalled_sec=0)
    slot = occupy(trial, 0, runner, username="recording_now")

    trial.pool_loader = lambda: ["other"]        # recording_now を無効化
    trial._last_pool_refresh = 0.0
    trial._maybe_refresh_pool()

    assert trial.pool == ["other"], "巡回対象から外れていない"
    assert slot.in_use is True, "録画中のスロットが解放された"
    assert runner.end_calls == [], "録画中のセッションが終了させられた"
