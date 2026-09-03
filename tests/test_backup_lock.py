"""バックアップの二重起動防止。

DBが7.2GBに育ちバックアップに12分かかるようになり、タイマーの定期実行と
手動実行が重なる余地ができた(以前74秒の頃は現実的に起きなかった)。
実際に発生したときの害はディスクとI/O -- 一時ファイルが倍(24GB)になり、
ディスク使用率が57%まで上がった。データ破損は起きない。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

import backup_db


def test_lock_is_acquired_and_released(tmp_path):
    lock = str(tmp_path / "x.lock")
    with backup_db.single_instance(lock) as ok:
        assert ok is True
        assert os.path.exists(lock)
    assert not os.path.exists(lock), "終了時にロックが残っている"


def test_second_run_is_refused_while_the_first_holds_the_lock(tmp_path):
    lock = str(tmp_path / "x.lock")
    with backup_db.single_instance(lock) as first:
        assert first is True
        with backup_db.single_instance(lock) as second:
            assert second is False, "二重起動を弾けていない"


def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path):
    """異常終了でロックが残っても、次回が永久に弾かれないこと。"""
    lock = str(tmp_path / "x.lock")
    # 存在しないPIDを書き込む(kill -0 で不在と分かる大きな値)
    with open(lock, "w") as f:
        f.write("999999")
    with backup_db.single_instance(lock) as ok:
        assert ok is True, "死んだプロセスのロックに阻まれている"


def test_a_stale_lock_is_taken_over_even_if_the_pid_is_alive(tmp_path):
    """PIDが再利用された場合に備え、時刻でも切る。"""
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write(str(os.getpid()))       # 生きているPID
    old = time.time() - backup_db.LOCK_STALE_SEC - 60
    os.utime(lock, (old, old))
    with backup_db.single_instance(lock) as ok:
        assert ok is True, "古いロックが解放されていない"


def test_a_fresh_lock_from_a_live_process_is_respected(tmp_path):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write(str(os.getpid()))       # 生きている & 新しい
    with backup_db.single_instance(lock) as ok:
        assert ok is False


def test_backup_is_not_blocked_by_an_unwritable_lock_path(tmp_path):
    """ロックが作れないことを理由にバックアップを止めない。
    二重起動の防止は利便性のためで、バックアップそのものより優先しない。"""
    lock = str(tmp_path / "no-such-dir" / "x.lock")
    with backup_db.single_instance(lock) as ok:
        assert ok is True


def test_skipping_is_not_an_error(tmp_path, monkeypatch, capsys):
    """既に実行中ならスキップして **正常終了** する。タイマー起動で
    失敗扱いにすると systemd の失敗通知が鳴り続ける。"""
    db = tmp_path / "live.db"
    from tiktok_monitor import db as dbmod
    conn = dbmod.connect(str(db)); dbmod.init_schema(conn); conn.close()

    lock = os.path.join(str(tmp_path), ".live.db" + backup_db.LOCK_PATH_SUFFIX)
    with open(lock, "w") as f:
        f.write(str(os.getpid()))       # 生きているPID = 実行中とみなされる

    monkeypatch.setattr(sys, "argv", [
        "backup_db.py", "--db-path", str(db), "--dest-dir", str(tmp_path), "--tier", "hourly",
    ])
    assert backup_db.main() == 0, "スキップがエラー終了になっている"
    # スキップしたのでバックアップファイルは作られない
    assert not [p for p in os.listdir(str(tmp_path)) if p.startswith("proxy5_")]
