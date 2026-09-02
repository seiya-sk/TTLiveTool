"""録画枠の不足を記録する(必要IP数を見積もるための実測)。

以前は全録画枠が埋まっている間、巡回ループが is_live チェックすら行わずに
眠っていた。そのため「枠が足りずに録り逃した配信」が1件も残らず、過去ログから
遡って再構成することもできなかった(チェック自体が走っていないため)。

実測(2026-09-01): 上限9本の状態が合計351分あり、その間の巡回チェックは2件。
通常ペースなら約4,213件に相当する。最長の連続区間は51分。

**枠不足の判定は「is_live=True かつ 空き枠なし」だけ。** 接続失敗や
クールダウン中の見送りは原因が違うので混ぜない -- 混ぜると必要IP数を
過大に見積もる。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

from tiktok_monitor import proxy_pool_trial as ppt
from tiktok_monitor import watch as w
from tests.test_ip_handoff import FakePacer, FakeRunner, make_trial, occupy


def fill_recording_slots(trial, n=9):
    for i in range(n):
        occupy(trial, i, FakeRunner(i + 1, f"ROOM_{i}"), username=f"busy{i}")


def read_events(trial):
    path = Path(trial.events_path)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_slot_shortage_is_recorded_when_a_live_cannot_be_dispatched(tmp_path):
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    trial.pool = ["someone_live"]
    fill_recording_slots(trial)
    assert trial._next_recording_slot() is None, "前提が崩れている(まだ録画枠がある)"

    asyncio.run(trial._scan_while_full())

    ev = [e for e in read_events(trial) if e["event"] == "slot_shortage"]
    assert len(ev) == 1, f"枠不足が記録されていない: {read_events(trial)}"
    assert ev[0]["username"] == "someone_live"
    assert ev[0]["active_count"] == 9
    assert ev[0]["max_recording_slots"] == 9
    assert ev[0]["total_slots"] == 10


def test_an_offline_streamer_is_not_counted_as_a_shortage(tmp_path):
    """配信していないなら枠不足の被害ではない。"""
    trial = make_trial(10, status=w.OFFLINE, tmp_path=tmp_path)
    trial.pool = ["someone_offline"]
    fill_recording_slots(trial)

    asyncio.run(trial._scan_while_full())

    assert [e for e in read_events(trial) if e["event"] == "slot_shortage"] == []


def test_unknown_check_is_not_counted_as_a_shortage(tmp_path):
    """確認できなかっただけ。配信していた証拠が無いので数えない。"""
    trial = make_trial(10, status=w.UNKNOWN, tmp_path=tmp_path)
    trial.pool = ["someone"]
    fill_recording_slots(trial)

    asyncio.run(trial._scan_while_full())

    assert [e for e in read_events(trial) if e["event"] == "slot_shortage"] == []


def test_cooldown_and_quarantine_are_not_counted_as_shortage(tmp_path):
    """**原因の違うものを混ぜない。** 初回接続のクールダウン中や隔離中の
    ライバーは、枠があっても繋ぎにいかない。これを枠不足として数えると
    必要IP数を過大に見積もる。"""
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    trial.pool = ["cooling", "quarantined"]
    fill_recording_slots(trial)
    now = time.monotonic()
    for _ in range(ppt.MAX_INITIAL_CONNECT_ATTEMPTS):
        trial._connect_gate.record_failure("cooling", "R", "InvalidStatusCode", 1, now)
    trial._quarantined.add("quarantined")

    asyncio.run(trial._scan_while_full())

    assert [e for e in read_events(trial) if e["event"] == "slot_shortage"] == [], \
        "原因の違う見送りを枠不足として記録した"


def test_repeated_skips_for_the_same_streamer_are_collapsed(tmp_path):
    """同一ライバーは巡回のたびに引っかかる。毎回書くとログが溢れる。"""
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    trial.pool = ["someone_live"]
    fill_recording_slots(trial)

    async def scenario():
        for _ in range(5):
            await trial._scan_while_full()

    asyncio.run(scenario())

    ev = [e for e in read_events(trial) if e["event"] == "slot_shortage"]
    assert len(ev) == 1, f"まとめられていない({len(ev)}件書かれた)"
    assert trial._shortage_pending["someone_live"] == 4, "まとめた回数を数えていない"


def test_the_collapsed_count_is_reported_after_the_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(ppt, "SLOT_SHORTAGE_LOG_INTERVAL_SEC", 0.0)
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    trial.pool = ["someone_live"]
    fill_recording_slots(trial)

    async def scenario():
        await trial._scan_while_full()
        trial._shortage_pending["someone_live"] = 7
        await trial._scan_while_full()

    asyncio.run(scenario())

    ev = [e for e in read_events(trial) if e["event"] == "slot_shortage"]
    assert ev[-1]["suppressed"] == 7, "まとめた件数が報告されていない"


def test_scanning_continues_while_full(tmp_path):
    """満杯の間も巡回が止まらないこと。止まると誰が配信していたか分からず、
    必要IP数の見積もりができない(実測351分ぶんのデータが失われた)。"""
    trial = make_trial(10, status=w.LIVE, tmp_path=tmp_path)
    trial.pool = ["a", "b", "c"]
    fill_recording_slots(trial)

    async def scenario():
        for _ in range(3):
            await trial._scan_while_full()

    asyncio.run(scenario())

    assert len(trial.pacer.checks) == 3, "満杯の間に巡回が止まっている"


# --- health_report 側 ------------------------------------------------------
def _shortage_event(ts, user, active=9, suppressed=0):
    return {"timestamp": ts, "event": "slot_shortage", "username": user,
            "active_count": active, "max_recording_slots": 9, "total_slots": 10,
            "suppressed": suppressed}


def test_health_report_counts_and_estimates_concurrent_streamers(capsys):
    import health_report as hr
    events = [
        _shortage_event("2026-09-01T12:00:00+00:00", "a", suppressed=2),
        _shortage_event("2026-09-01T12:01:00+00:00", "b"),
        _shortage_event("2026-09-01T12:02:00+00:00", "c"),
    ]
    result = hr.report_slot_shortage(events)

    assert result["count"] == 5, "まとめられた件数が合計に入っていない"
    assert result["logged"] == 3
    assert {s["username"] for s in result["streamers"]} == {"a", "b", "c"}
    # 録画中9本 + 見送り3人 = 同時12人が配信していたと推定される
    assert result["peak_concurrent_estimate"] == 12, result["peak_concurrent_estimate"]
    out = capsys.readouterr().out
    assert "@a" in out and "@b" in out and "@c" in out


def test_health_report_distinguishes_no_shortage_from_no_data(capsys):
    """記録0件でも、上限に張り付いていたなら「不足なし」とは言えない。
    満杯中の巡回記録が無い期間(機能追加前)は判定不能と明示する。"""
    import health_report as hr
    marks = []
    for i in range(9):
        marks.append({"timestamp": f"2026-09-01T12:0{i}:00+00:00", "event": "recording_started"})
    marks.append({"timestamp": "2026-09-01T13:00:00+00:00", "event": "recording_ended"})

    result = hr.report_slot_shortage(marks)
    out = capsys.readouterr().out

    assert result["count"] == 0
    assert result["measurable"] is False, "判定不能を「不足なし」と報告している"
    assert "判定できない" in out
    assert result["at_capacity_sec"] > 0
