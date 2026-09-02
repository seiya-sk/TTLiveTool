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
