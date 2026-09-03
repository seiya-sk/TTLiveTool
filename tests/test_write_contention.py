"""書き込み競合でイベントを失わないこと(2026-09-02 の障害の再発防止)。

掃除ジョブ(cleanup_raw_payloads)が数万行を1トランザクションで消していた間、
録画側の INSERT が "database is locked" で失敗し、一晩でイベント24件が
失われた。掃除の所要は最長47.7秒で、録画側の busy_timeout(既定5秒)を
大きく超えていた。

対策は3段:
  1. 掃除の DELETE をバッチ分割し、ロック保持を短く保つ
  2. busy_timeout を延ばす
  3. それでも書けなければ再試行し、最後はファイルへ退避する(捨てない)
"""
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tiktok_monitor import client as client_module
from tiktok_monitor import cleanup_raw_payloads as cleanup
from tiktok_monitor import db


def make_db(path, sessions=2, events_per_session=400, payload_bytes=4000):
    conn = db.connect(str(path))
    db.init_schema(conn)
    sid_list = []
    for i in range(sessions):
        sid = db.create_live_session(conn, db.get_or_create_streamer(conn, f"user{i}"))
        db.end_session(conn, sid, "live_end")
        conn.execute("UPDATE live_sessions SET ended_at = ? WHERE id = ?",
                     ("2020-01-01T00:00:00+00:00", sid))
        sid_list.append(sid)
        for _ in range(events_per_session):
            db.insert_event(conn, sid, "comment", None, None,
                            {"text": "x"}, {"raw": "y" * payload_bytes})
    conn.commit()
    return conn, sid_list


def test_busy_timeout_is_set_generously():
    """既定5秒では足りなかった。待つのは無害、失敗はデータ損失。"""
    conn = db.connect(":memory:")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000


def test_batched_delete_removes_everything_and_makes_progress(tmp_path):
    """rowid で消すのが要点。条件付き LIMIT DELETE を繰り返すと
    live_events 側が残るため同じ行が何度も選ばれ、進まなくなる。"""
    conn, sids = make_db(tmp_path / "a.db", sessions=2, events_per_session=250)
    before = conn.execute("SELECT COUNT(*) FROM live_event_raw_payloads").fetchone()[0]
    assert before == 500

    deleted = cleanup.delete_raw_payloads_batched(conn, sids, batch_rows=50, pause_sec=0)

    assert deleted == 500
    assert conn.execute("SELECT COUNT(*) FROM live_event_raw_payloads").fetchone()[0] == 0
    # 本体のイベントは消さない
    assert conn.execute("SELECT COUNT(*) FROM live_events").fetchone()[0] == 500


def test_recording_keeps_writing_while_cleanup_runs(tmp_path):
    """**本命**: 掃除が走っている最中でも、録画側の書き込みが1件も落ちないこと。

    以前の一括DELETEでは、この状況で "database is locked" が出て
    イベントが失われていた。
    """
    db_path = tmp_path / "live.db"
    conn, sids = make_db(db_path, sessions=3, events_per_session=500, payload_bytes=8000)
    live_sid = db.create_live_session(conn, db.get_or_create_streamer(conn, "recording_now"))

    failures = []
    written = []
    stop = threading.Event()

    def recorder():
        # sqlite3 の接続はスレッドをまたげないので、書き込み側スレッドの中で開く
        writer_conn = db.connect(str(db_path))
        i = 0
        while not stop.is_set():
            try:
                db.insert_event(writer_conn, live_sid, "comment", None, None,
                                {"n": i}, {"raw": "z" * 500})
                written.append(i)
            except Exception as exc:      # WriteFailed も含めて捕える
                failures.append(repr(exc))
            i += 1
            time.sleep(0.002)

    t = threading.Thread(target=recorder, daemon=True)
    t.start()
    time.sleep(0.05)
    cleanup.delete_raw_payloads_batched(conn, sids, batch_rows=100, pause_sec=0.01)
    time.sleep(0.05)
    stop.set()
    t.join(timeout=10)

    assert failures == [], f"掃除中に書き込みが失敗した: {failures[:3]}"
    assert len(written) > 20, f"テストの前提が弱い(書き込み {len(written)}件のみ)"
    stored = conn.execute(
        "SELECT COUNT(*) FROM live_events WHERE live_session_id = ?", (live_sid,)
    ).fetchone()[0]
    assert stored == len(written), "書き込んだつもりの件数とDBの件数が合わない"


def test_batched_delete_never_holds_the_lock_long(tmp_path):
    """**この試験が本命の保証**: 削除中に書き込みロックが長時間占有されないこと。

    「掃除中に書けるか」を件数で見る試験は、テストデータが小さいと旧実装でも
    通ってしまう(実測: 14MBでは一括DELETEでも0.06秒で終わり競合しない)。
    規模に依存しない性質はロックの保持時間そのもの。

    実測(140MB, 2026-09-03):
        一括DELETE  0.76秒を連続で占有(9回ブロック)
        バッチ      最大0.05秒(1回)
    本番規模(430MB / 47.7秒)では、一括だと busy_timeout を確実に超える。
    """
    db_path = tmp_path / "lock.db"
    conn, sids = make_db(db_path, sessions=3, events_per_session=400, payload_bytes=6000)

    blocked: list[float] = []
    stop = threading.Event()

    def probe():
        # 書き込みロックを取りにいき、取れなかった時間を測る
        p = sqlite3.connect(str(db_path), timeout=0.05)
        while not stop.is_set():
            t0 = time.time()
            try:
                p.execute("BEGIN IMMEDIATE")
                p.rollback()
            except sqlite3.OperationalError:
                blocked.append(time.time() - t0)
            time.sleep(0.005)
        p.close()

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    time.sleep(0.05)
    cleanup.delete_raw_payloads_batched(conn, sids, batch_rows=100, pause_sec=0.02)
    stop.set()
    t.join(timeout=5)

    worst = max(blocked) if blocked else 0.0
    assert worst < 1.0, f"ロックを{worst:.2f}秒占有した(バッチが効いていない)"


def test_batch_size_adapts_to_keep_the_lock_hold_under_target(tmp_path):
    """**行数ではなく時間で決めること。**

    当初は「500行ずつ」の固定にしていたが、本番では1バッチ574msかかった
    (2026-09-03 実測)。合成テストの生ペイロードが3KBだったのに対し本番は
    約23KB -- 22倍重く、行数で決めた見積もりが外れた。ペイロードの大きさは
    配信内容で変わるので、行数固定では同じ外し方を繰り返す。

    ここでは本番相当の23KBで、実測のロック保持が目標に収まることを見る。
    """
    conn, sids = make_db(tmp_path / "adapt.db", sessions=2, events_per_session=300,
                         payload_bytes=23_000)
    stats: dict = {}
    deleted = cleanup.delete_raw_payloads_batched(conn, sids, pause_sec=0, stats=stats)

    assert deleted == 600
    assert stats["worst_lock_ms"] <= 200, (
        f"1バッチのロック保持が {stats['worst_lock_ms']}ms。"
        f"目標 {cleanup.DELETE_BATCH_TARGET_MS}ms に寄せられていない"
    )
    assert stats["batches"] >= 2, "1バッチで消し切っている(分割が効いていない)"


def test_batch_size_stays_within_bounds(tmp_path):
    """適応させても極端な行数にしないこと。細切れはオーバーヘッドだけ増える。"""
    conn, sids = make_db(tmp_path / "bounds.db", sessions=1, events_per_session=200,
                         payload_bytes=200)
    stats: dict = {}
    cleanup.delete_raw_payloads_batched(conn, sids, pause_sec=0, stats=stats)
    assert cleanup.DELETE_BATCH_MIN_ROWS <= stats["last_batch_rows"] <= cleanup.DELETE_BATCH_MAX_ROWS


def test_target_is_tight_enough_to_matter():
    """目標が緩められていないことを固定する。"""
    assert cleanup.DELETE_BATCH_TARGET_MS <= 100
    assert cleanup.DELETE_BATCH_START_ROWS <= 100, "初回バッチが大きいと1回目で超過する"


def test_write_retries_then_succeeds_when_the_lock_clears(tmp_path):
    """ロックが明ければ再試行で書けること(諦めが早すぎないこと)。"""
    db_path = tmp_path / "b.db"
    conn, _ = make_db(db_path, sessions=1, events_per_session=1)
    blocker = sqlite3.connect(str(db_path), timeout=0.1, check_same_thread=False)
    blocker.execute("BEGIN IMMEDIATE")        # 書き込みロックを占有

    writer = db.connect(str(db_path))
    writer.execute("PRAGMA busy_timeout = 200")   # 待ちを短くして再試行を試す
    result = {}

    def release_later():
        time.sleep(0.6)
        blocker.rollback()
        blocker.close()

    threading.Thread(target=release_later, daemon=True).start()
    sid = conn.execute("SELECT id FROM live_sessions LIMIT 1").fetchone()[0]
    result["id"] = db.insert_event(writer, sid, "comment", None, None, {"a": 1}, {"b": 2})

    assert result["id"] is not None, "ロックが明けたのに書けていない"


def test_write_failure_raises_instead_of_silently_dropping(tmp_path):
    """再試行を使い切ったら **例外を投げる**。None を返して黙って進まない。"""
    db_path = tmp_path / "c.db"
    conn, _ = make_db(db_path, sessions=1, events_per_session=1)
    blocker = sqlite3.connect(str(db_path), timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")        # 最後まで解放しない

    writer = db.connect(str(db_path))
    writer.execute("PRAGMA busy_timeout = 50")
    sid = conn.execute("SELECT id FROM live_sessions LIMIT 1").fetchone()[0]

    with pytest.raises(db.WriteFailed):
        db.write_with_retry(
            writer,
            lambda: db._insert_event_once(writer, sid, "comment", None, None,
                                          {"a": 1}, {"b": 2}, None),
            retries=2, base_sec=0.01,
        )
    blocker.rollback(); blocker.close()


def test_non_lock_errors_are_not_retried(tmp_path):
    """制約違反などは再試行しても直らない。無駄に待たず即座に投げる。"""
    conn = db.connect(":memory:")
    db.init_schema(conn)
    calls = []

    def boom():
        calls.append(1)
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError):
        db.write_with_retry(conn, boom, retries=3, base_sec=0)
    assert len(calls) == 1, f"ロック以外でも再試行している({len(calls)}回)"


def test_failed_event_is_written_to_the_recovery_file(tmp_path, monkeypatch):
    """書けなかったイベントは **必ず** ファイルに残す。握りつぶさない。"""
    path = tmp_path / "failed_events.jsonl"
    monkeypatch.setattr(client_module, "FAILED_EVENT_LOG_PATH", str(path))

    client_module._record_failed_event({
        "timestamp": "2026-09-03T00:00:00+00:00",
        "reason": "database is locked",
        "username": "someone",
        "live_session_id": 42,
        "event_type": "gift",
        "user_id": "u1",
        "user_nickname": "nick",
        "payload": {"diamond_count": 100},
        "raw_payload": {"raw": "x"},
        "occurred_at": "2026-09-03T00:00:00+00:00",
    })

    entries = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) == 1
    e = entries[0]
    # 復元に必要な列がすべて残っていること
    for key in ("live_session_id", "event_type", "user_id", "user_nickname",
                "payload", "raw_payload", "occurred_at"):
        assert key in e, f"{key} が退避されていない(復元できない)"
    assert e["payload"]["diamond_count"] == 100


def test_record_event_falls_back_instead_of_losing_the_event(tmp_path, monkeypatch):
    """_record_event が WriteFailed を受けたら退避まで行うこと。"""
    path = tmp_path / "failed.jsonl"
    monkeypatch.setattr(client_module, "FAILED_EVENT_LOG_PATH", str(path))

    from tiktok_monitor.config import Settings
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(username="someone", db_path=":memory:", idle_timeout_sec=60,
                        screenshots_enabled=False)
    runner = client_module.SessionRunner(conn, settings)
    runner.streamer_id = db.get_or_create_streamer(conn, "someone")
    runner.live_session_id = db.create_live_session(conn, runner.streamer_id)
    runner._ensure_session = lambda: None
    runner.note_event = lambda: None

    def always_locked(*a, **kw):
        raise db.WriteFailed("database is locked")

    monkeypatch.setattr(db, "insert_event", always_locked)
    runner._record_event(lambda e: ("gift", "u1", "nick", {"diamond_count": 5}), object())

    entries = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) == 1, "WriteFailed なのに退避されていない"
    assert entries[0]["event_type"] == "gift"


def test_all_event_writes_go_through_the_safe_helper():
    """イベントを書く経路が退避処理を必ず通ること。

    直に db.insert_event を呼ぶ経路が増えると、そこだけ WriteFailed が
    握りつぶされてイベントが失われる(2026-09-02 に失った24件と同じ結末)。
    ヘルパ以外からの直接呼び出しが増えていないことを固定する。
    """
    source = (Path(__file__).resolve().parents[1] / "tiktok_monitor" / "client.py").read_text(
        encoding="utf-8")
    direct = [ln.strip() for ln in source.splitlines() if "db.insert_event(" in ln]
    assert len(direct) == 1, (
        f"db.insert_event の直接呼び出しが {len(direct)} 箇所ある。"
        f"_insert_event_safely 経由にすること: {direct}"
    )


def test_battle_and_treasure_box_also_fall_back(tmp_path, monkeypatch):
    """ギフト以外の経路(バトル・宝箱)でも退避されること。"""
    path = tmp_path / "failed.jsonl"
    monkeypatch.setattr(client_module, "FAILED_EVENT_LOG_PATH", str(path))
    from tiktok_monitor.config import Settings

    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(username="someone", db_path=":memory:", idle_timeout_sec=60,
                        screenshots_enabled=False)
    runner = client_module.SessionRunner(conn, settings)
    runner.streamer_id = db.get_or_create_streamer(conn, "someone")
    runner.live_session_id = db.create_live_session(conn, runner.streamer_id)

    monkeypatch.setattr(db, "insert_event",
                        lambda *a, **kw: (_ for _ in ()).throw(db.WriteFailed("locked")))

    ok = runner._insert_event_safely("battle_opponent", "op1", None,
                                     {"opponent_id": "op1"}, {"raw": 1}, None)
    assert ok is False
    entries = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert entries[0]["event_type"] == "battle_opponent"


def test_batch_sizes_do_not_oscillate_between_the_limits(tmp_path):
    """適応方式が上下限を **往復** しないこと。

    見るべきは行数そのものではなく、ロック保持が予算に収まっていること。
    行数が上限(または下限)に落ち着くのは正常 -- 1行が軽ければ大きく、
    重ければ小さくなるだけで、それが適応の目的。
    問題なのは 25 と 1000 を交互に行き来する状態で、そのとき大きい側の
    バッチがロックを長く持つ。

    実測の推移は [50, 184, 566] のように控えめな初期値から目標へ
    上がっていく形になる(2026-09-03)。
    """
    conn, sids = make_db(tmp_path / "conv.db", sessions=2, events_per_session=400,
                         payload_bytes=23_000)
    stats: dict = {}
    cleanup.delete_raw_payloads_batched(conn, sids, pause_sec=0, stats=stats)

    sizes = [s for s in stats["batch_sizes"] if isinstance(s, int)]
    assert sizes, "バッチが1回も走っていない"
    lo, hi = cleanup.DELETE_BATCH_MIN_ROWS, cleanup.DELETE_BATCH_MAX_ROWS
    for a, b in zip(sizes, sizes[1:]):
        assert not (a == lo and b == hi), f"下限と上限を往復している: {sizes}"
        assert not (a == hi and b == lo), f"上限と下限を往復している: {sizes}"
    # 適応の結果として、ロック保持が予算に収まっていることが本題
    assert stats["worst_lock_ms"] <= 200, stats
