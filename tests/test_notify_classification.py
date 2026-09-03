"""署名枯渇を「プロキシ異常」と混ぜないこと(A-1 の残件)。

2026-09-01、Euler Stream の日次上限に当たって22時〜24時のイベントが0件に
なった。当時この事象は proxy.degraded(タイトル「プロキシ異常」)として
通知されていた。プロキシは正常で、IPを替えても解決しない事象なので、
その分類は調査を誤った方向へ誘導する。

あわせて「原因を問わずデータが取れていないこと」を通知する種別も固定する。
署名枯渇(A-1)と上流ネットワーク断(A-3)は原因が違うが症状は同じで、
原因ごとの検知は必ず漏れるため。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

import pytest

import error_notifier as en


def write_events(tmp_path, records):
    p = tmp_path / "events.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return str(p)


SIGNATURE_EVENT = {
    "timestamp": "2026-09-01T12:40:00+00:00", "event": "status",
    "kind": "signature_rate_limit", "username": "someone", "proxy": "1.2.3.4:8080",
    "info": {"wait_seconds": 61605, "reset_time": 61605},
}
PROXY_EVENT = {
    "timestamp": "2026-09-01T12:41:00+00:00", "event": "check_error",
    "username": "someone", "proxy": "5.6.7.8:8080", "error": "ConnectTimeout('')",
}


def test_signature_exhaustion_is_not_classified_as_a_proxy_problem(tmp_path):
    lines = [json.dumps(SIGNATURE_EVENT)]
    collected = en.collect_from_events(lines)

    assert len(collected["signature_limits"]) == 1, "署名枯渇が拾えていない"
    assert collected["proxy_errors"] == [], \
        "署名枯渇がプロキシ異常に混ざっている(IPを疑う方向へ調査を誘導する)"


def test_a_real_proxy_error_still_lands_in_proxy_degraded(tmp_path):
    """分離のしすぎで本物のプロキシ異常を取りこぼしていないこと。"""
    collected = en.collect_from_events([json.dumps(PROXY_EVENT)])
    assert len(collected["proxy_errors"]) == 1
    assert collected["signature_limits"] == []


def test_the_two_are_separated_when_both_happen(tmp_path):
    collected = en.collect_from_events([json.dumps(SIGNATURE_EVENT), json.dumps(PROXY_EVENT)])
    assert len(collected["signature_limits"]) == 1
    assert len(collected["proxy_errors"]) == 1


def test_signature_rule_exists_and_is_not_throttled_for_too_long():
    """録画が止まっている間の通知なので、抑止時間を長くしすぎない。"""
    rule = en.ERROR_RULES["signature.exhausted"]
    assert rule["throttle_sec"] <= 1800
    assert "署名" in rule["title"]
    assert "プロキシ" not in rule["title"], "タイトルにプロキシが残っている"


def test_signature_message_says_it_is_not_a_proxy_problem():
    """深夜に届いた通知が朝まで放置されないよう、一行目で状況を伝える。"""
    body = en.build_signature_message([dict(SIGNATURE_EVENT, wait_sec=61605)], suppressed=0)
    assert "録画" in body.split("[/title]")[0], "タイトルに影響が書かれていない"
    assert "プロキシの異常ではありません" in body
    assert "IPを替えても解決しません" in body


def test_blackout_rule_exists():
    assert "data.blackout" in en.ERROR_RULES


def test_blackout_message_states_it_is_cause_agnostic():
    body = en.build_blackout_message({
        "window_min": 5, "ratio_pct": 5, "baseline": 656.0,
        "recent": 0, "recording": 0, "live_seen": 3, "minutes": 62,
    })
    assert "原因は問いません" in body
    assert "署名枯渇" in body and "上流障害" in body


def test_blackout_check_returns_none_when_there_is_no_data(tmp_path):
    """イベントが無い環境で例外を投げたり、誤って通知したりしないこと。"""
    db = tmp_path / "empty.db"
    from tiktok_monitor import db as dbmod
    conn = dbmod.connect(str(db))
    dbmod.init_schema(conn)
    conn.close()
    assert en.check_blackout(str(db), write_events(tmp_path, [])) is None


# --- proxy.degraded の閾値(2026-09-03 改定)--------------------------------
def _errs(n, proxy="1.1.1.1:8080"):
    return [{"error_class": "ConnectTimeout", "username": f"u{i}",
             "proxy": proxy, "timestamp": "2026-09-03T00:00:00+00:00"} for i in range(n)]


def test_a_few_timeouts_no_longer_trigger_an_alert():
    """数件の ReadTimeout で「プロキシ異常」が飛ぶのは過剰。
    平常運転(0.51%)でも1分あたり数件は出る。"""
    assert en.proxy_degraded_reasons(_errs(3), check_total=700) == []
    assert en.proxy_degraded_reasons(_errs(9), check_total=700) == []


def test_widespread_errors_trigger_an_alert():
    """5本以上のIPが同時に被弾 = 上流の障害を疑う。"""
    errs = [e for i in range(5) for e in _errs(1, proxy=f"10.0.0.{i}:8080")]
    reasons = en.proxy_degraded_reasons(errs, check_total=700)
    assert reasons and "5本" in reasons[0]


def test_concentration_on_one_ip_triggers_an_alert():
    """単一IPに10件集中 = そのIPの劣化。"""
    reasons = en.proxy_degraded_reasons(_errs(10), check_total=700)
    assert any("単一IP" in r for r in reasons)


def test_high_error_rate_triggers_an_alert():
    """平常の10倍(5.1%)以上。分母は満杯中の巡回も含む。"""
    reasons = en.proxy_degraded_reasons(_errs(6), check_total=100)   # 6%
    assert any("エラー率" in r for r in reasons)


def test_rate_rule_needs_a_big_enough_sample():
    """試行が少ないうちは率で判定しない。1/3=33% で警報が飛ぶのを防ぐ。"""
    assert en.proxy_degraded_reasons(_errs(1), check_total=3) == []
    assert en.proxy_degraded_reasons(_errs(2), check_total=10) == []


def test_the_alert_says_why_it_fired():
    """IPを替えるべきか上流を疑うべきかが本文から分かること。"""
    errs = [e for i in range(6) for e in _errs(2, proxy=f"10.0.0.{i}:8080")]
    reasons = en.proxy_degraded_reasons(errs, check_total=700)
    body = en.build_proxy_message(errs, suppressed=0, reasons=reasons)
    assert "通知した理由" in body
    assert "上流の障害を疑う" in body


def test_check_events_are_counted_as_the_denominator():
    """満杯中の巡回(while_full)も分母に入ること。"""
    import json as _json
    lines = [_json.dumps({"timestamp": "2026-09-03T00:00:00+00:00", "event": "check",
                          "username": "u", "is_live": False, "while_full": True})
             for _ in range(80)]
    collected = en.collect_from_events(lines)
    assert collected["check_total"] == 80
