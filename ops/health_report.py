#!/usr/bin/env python3
"""長期テストの定期健診レポート(読み取りのみ)。

いつ実行しても現状が1画面で分かることを狙う。稼働中のDB/ログには
読み取りしかせず、録画プロセスには一切触れない。

見るもの:
  1. IP別のエラー率      -- 特定IPだけ恒常的に悪化していないか
  2. 全体ベースライン     -- 平常時が0.5%を超えて定着していないか
  3. 新ロジックの検証     -- セッション分割の根絶 / 終了種別 / IP乗り換え
  4. 署名消費ペース       -- 枯渇の安全域に留まっているか

ベースラインには「時間別エラー率の中央値」を使う。単純な平均だと、
2026-09-01 16:50 のような10分間のスパイク(VPSのリソース競合が原因で、
IP品質とは無関係)1回で全体が跳ね上がり、恒常的な劣化と区別できない。
中央値なら単発のスパイクに引きずられない。
"""
import argparse
import collections
import glob
import json
import os
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 良性として除外する例外。実データ由来:
#   UserNotFoundError -- streamers.txt に残った削除済みアカウント(常時発生)
#   UserOfflineError  -- 単に配信していないだけ
BENIGN_ERRORS = {"UserNotFoundError", "UserOfflineError"}

BASELINE_WARN_PCT = 0.5      # これを超えて定着したら全体的な劣化のサイン
IP_WARN_PCT = 2.0            # 単独IPがこれを超え続けるなら交換を検討
# Euler Stream のレート制限は **IP単位**(2026-09-02 実測)。
#   day 100 / hour 30 / minute 5
# 以前ここには「600回/時」という瞬間レートの危険水準しか無く、日次の累計を
# 見ていなかった。そのため実際には枯渇していたのに「安全域1.3%」と報告し
# 続け、2時間半のデータ欠損に気づけなかった。日次残量を直接見るように変更。
SIGNATURE_DAY_MAX = 100
SIGNATURE_WARN_REMAINING_PCT = 30   # 残り30%を切ったら警告
RATE_LIMIT_URL = "https://api.eulerstream.com/webcast/rate_limits"


def _jst(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(JST)


def error_class(event: dict) -> str | None:
    """check_error / connection_error から例外クラス名を取り出す。"""
    if event.get("event") == "check_error":
        return str(event.get("error", "")).split("(")[0] or None
    if event.get("kind") == "connection_error":
        return str((event.get("info") or {}).get("error", "")).split("(")[0] or None
    return None


def load_events(path: str, since: datetime) -> list[dict]:
    events = []
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if _jst(e["timestamp"]) >= since:
                    events.append(e)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return events


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


SPIKE_HOUR_PCT = 5.0   # この率を超えた時間帯は「スパイク」として別扱いにする


def report_error_rates(events: list[dict]) -> dict:
    section("1. エラー率(良性の UserNotFound/UserOffline は除外)")

    # 時間帯ごとに、全体とIP別を同時に積む。IP別の判定を平常時だけに
    # 絞れるようにするため、IP別も時間帯で分けて保持する。
    hourly = collections.OrderedDict()
    hourly_ip_checks = collections.defaultdict(collections.Counter)
    hourly_ip_errors = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    total_checks = total_errors = 0

    for e in events:
        hour = _jst(e["timestamp"]).strftime("%m/%d %H時")
        bucket = hourly.setdefault(hour, collections.Counter())
        proxy = e.get("proxy")
        if e.get("event") == "check":
            bucket["check"] += 1
            total_checks += 1
            if proxy:
                hourly_ip_checks[hour][proxy] += 1
        cls = error_class(e)
        if cls and cls not in BENIGN_ERRORS:
            bucket["err"] += 1
            total_errors += 1
            if proxy:
                hourly_ip_errors[hour][proxy][cls] += 1

    print(f"\n  {'時間帯':14} {'check':>6} {'エラー':>6} {'率':>7}")
    rates = []
    for hour, b in hourly.items():
        if not b["check"]:
            continue
        rate = b["err"] / b["check"] * 100
        rates.append(rate)
        flag = "  ← スパイク" if rate >= 5 else ""
        print(f"  {hour:14} {b['check']:>6} {b['err']:>6} {rate:>6.2f}%{flag}")

    median = statistics.median(rates) if rates else 0.0
    mean = total_errors / total_checks * 100 if total_checks else 0.0
    print(f"\n  ベースライン(時間別の中央値): {median:.2f}%   "
          f"{'OK' if median < BASELINE_WARN_PCT else '★要注意(0.5%超で定着)'}")
    print(f"  単純平均(スパイク込み)      : {mean:.2f}%")

    # IP別は「平常時」だけで判定する。スパイク時間帯は全IPが同時に等しく
    # 被弾するため(2026-09-01 16:50 の実例: VPSのリソース競合で10IP全滅)、
    # 含めたままだと全IPに警告が付いて「特定IPの劣化」を検出できなくなる。
    quiet_hours = [h for h, b in hourly.items()
                   if b["check"] and b["err"] / b["check"] * 100 < SPIKE_HOUR_PCT]
    spike_hours = [h for h in hourly if h not in quiet_hours]

    per_ip_checks = collections.Counter()
    per_ip_errors = collections.defaultdict(collections.Counter)
    for hour in quiet_hours:
        for proxy, n in hourly_ip_checks[hour].items():
            per_ip_checks[proxy] += n
        for proxy, counter in hourly_ip_errors[hour].items():
            per_ip_errors[proxy].update(counter)

    label = f"平常時のみ / スパイク除外: {len(spike_hours)}時間帯 {spike_hours}" if spike_hours else "平常時のみ"
    print(f"\n  --- IP別({label}) ---")
    print(f"  {'IP':24} {'check':>6} {'エラー':>6} {'率':>7}  内訳")
    ip_rows = []
    flagged = 0
    for proxy in sorted(set(per_ip_checks) | set(per_ip_errors),
                        key=lambda p: -(sum(per_ip_errors[p].values()) / per_ip_checks[p] if per_ip_checks[p] else 0)):
        checks = per_ip_checks[proxy]
        errs = sum(per_ip_errors[proxy].values())
        rate = errs / checks * 100 if checks else 0.0
        flag = "  ★" if checks >= 50 and rate >= IP_WARN_PCT else ""
        if flag:
            flagged += 1
        print(f"  {proxy:24} {checks:>6} {errs:>6} {rate:>6.2f}%  {dict(per_ip_errors[proxy]) or '-'}{flag}")
        ip_rows.append({"proxy": proxy, "checks": checks, "errors": errs, "rate_pct": round(rate, 2)})
    print(f"\n  ★ = check 50回以上かつエラー率 {IP_WARN_PCT}% 超 → {flagged}件"
          f"{'(継続するなら交換を検討)' if flagged else ' ✓'}")
    if spike_hours:
        print(f"  ※ スパイク時間帯は全IPが同時に被弾する性質があり、IP品質の判定には使えないため除外")

    return {"baseline_median_pct": round(median, 3), "mean_pct": round(mean, 3),
            "total_checks": total_checks, "total_errors": total_errors,
            "spike_hours": spike_hours, "flagged_ips": flagged, "per_ip_quiet": ip_rows}


def report_sessions(db_path: str, since: datetime) -> dict:
    section("2. 新ロジックの検証(セッション分割・終了種別)")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    since_utc = since.astimezone(timezone.utc).isoformat()

    print("\n  --- end_detection_type の分布 ---")
    print("  auto が消え live_end / normal_closure / verified_offline が出ていれば、"
          "\n  終了判定が『こちらの推測』から『TikTokの明示 or 実確認』に移行できている")
    dist = {}
    for status, etype, n in conn.execute(
        "SELECT status, end_detection_type, COUNT(*) FROM live_sessions "
        "WHERE started_at >= ? GROUP BY 1,2 ORDER BY 3 DESC", (since_utc,)
    ):
        dist[f"{status}/{etype}"] = n
        print(f"    status={status:6} end_type={str(etype):16} {n:>4}件")
    if not dist:
        print("    (対象期間にセッションなし)")

    print("\n  --- 同一 room_id が複数セッションに分かれていないか ---")
    splits = conn.execute(
        """
        SELECT st.name, s.room_id, COUNT(*) n, GROUP_CONCAT(s.id)
        FROM live_sessions s JOIN streamers st ON st.id = s.streamer_id
        WHERE s.started_at >= ? AND s.room_id IS NOT NULL
        GROUP BY s.streamer_id, s.room_id HAVING n > 1 ORDER BY n DESC
        """, (since_utc,)
    ).fetchall()
    if splits:
        for name, room, n, ids in splits:
            print(f"    ★分割 {name:20} room_id={room} → {n}セッション ({ids})")
    else:
        print("    分割なし ✓(同じ room_id が複数セッションに分かれていない)")

    total = conn.execute(
        "SELECT COUNT(*) FROM live_sessions WHERE started_at >= ?", (since_utc,)
    ).fetchone()[0]
    with_room = conn.execute(
        "SELECT COUNT(*) FROM live_sessions WHERE started_at >= ? AND room_id IS NOT NULL", (since_utc,)
    ).fetchone()[0]
    # room_id 列が存在しなかった時期のセッションは構造的に NULL なので、
    # 記録率の分母から外す。含めると新ロジック稼働後も永久に 100% にならず、
    # 「未記録がある」という警告が意味を失う。
    first_with_room = conn.execute(
        "SELECT MIN(started_at) FROM live_sessions WHERE room_id IS NOT NULL"
    ).fetchone()[0]
    if first_with_room:
        after = conn.execute(
            "SELECT COUNT(*) FROM live_sessions WHERE started_at >= ?", (first_with_room,)
        ).fetchone()[0]
        after_with = conn.execute(
            "SELECT COUNT(*) FROM live_sessions WHERE started_at >= ? AND room_id IS NOT NULL",
            (first_with_room,),
        ).fetchone()[0]
        legacy = total - after if total >= after else 0
        print(f"\n  room_id 記録率(新ロジック稼働後): {after_with}/{after}"
              f"{' ✓' if after and after_with == after else '  ← 未記録があると継続判定できない'}")
        if legacy:
            print(f"  (うち {legacy} 件は room_id 列が無かった時期のセッションのため対象外)")
    else:
        print(f"\n  room_id 記録率: {with_room}/{total}  (まだ記録されたセッションがありません)")
    conn.close()
    return {"end_types": dist, "splits": len(splits), "sessions": total, "with_room_id": with_room}


def report_handoffs(events: list[dict]) -> dict:
    section("3. IP乗り換え(ストール検知と継続)")
    kinds = collections.Counter(e.get("event") for e in events)
    stall = [e for e in events if e.get("event") == "stall_check"]
    started = [e for e in events if e.get("event") == "handoff_started"]
    skipped = [e for e in events if e.get("event") == "handoff_skipped"]
    no_ip = [e for e in events if e.get("event") == "stall_check_skipped"]

    print(f"\n  生存確認 (stall_check)   : {len(stall):>4}件")
    print(f"  乗り換え (handoff_started): {len(started):>4}件")
    print(f"  乗り換え見送り            : {len(skipped):>4}件  "
          f"{dict(collections.Counter(e.get('reason') for e in skipped)) if skipped else ''}")
    print(f"  空きIPなしで確認できず    : {len(no_ip):>4}件"
          f"{'  ← 予約枠が効いていれば0のはず' if no_ip else ' ✓'}")

    if stall:
        live = sum(1 for e in stall if e.get("is_live"))
        print(f"\n  生存確認の内訳: まだライブ {live}件 / 終了確認 {len(stall) - live}件")
        print("\n  --- 直近の乗り換え ---")
        for e in started[-5:]:
            print(f"    {_jst(e['timestamp']):%H:%M:%S} @{e.get('username')} "
                  f"ip#{e.get('ip_index')}→ip#{e.get('to_ip_index')} "
                  f"session={e.get('live_session_id')} (#{e.get('handoff_count')})")
    else:
        print("\n  まだ発火していません(60秒以上の無イベントが起きていない = 正常)")
    return {"stall_checks": len(stall), "handoffs": len(started),
            "handoff_skipped": len(skipped), "no_free_ip": len(no_ip),
            "event_kinds": dict(kinds)}


def _fetch_rate_limits(proxy_url: str | None):
    """Euler Stream にその経路の残量を問い合わせる。署名は消費しない
    (単なる情報取得エンドポイント)。SIGN_API_KEY があれば付けて問い合わせる
    -- キーの有無でレート制限の単位(IP単位/アカウント単位)が変わるため、
    録画プロセスと同じ条件で見ないと意味がない。"""
    try:
        import httpx
        headers = {"User-Agent": "TikTokLive.py/7.0.0"}
        key = os.environ.get("SIGN_API_KEY", "").strip()
        if key:
            headers["X-Api-Key"] = key
        r = httpx.get(RATE_LIMIT_URL, proxy=proxy_url, timeout=15, headers=headers)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def report_signatures(events: list[dict], proxies_file: str) -> dict:
    section("4. 署名(Euler Stream)の残量 -- IP単位の日次上限")
    connects = [e for e in events if e.get("kind") == "connected"]
    limited = [e for e in events if e.get("kind") == "signature_rate_limit"]
    span_h = max(0.01, (_jst(events[-1]["timestamp"]) - _jst(events[0]["timestamp"])).total_seconds() / 3600) \
        if events else 0.01

    print(f"\n  接続(=署名消費): {len(connects)}回 / {span_h:.1f}時間 = {len(connects)/span_h:.1f}回/時")
    print(f"  レート制限に当たった回数: {len(limited)}回"
          f"{'  ← 発生している' if limited else ' ✓'}")

    api_key = os.environ.get("SIGN_API_KEY", "").strip()
    print(f"  SIGN_API_KEY: {'設定あり' if api_key else '未設定(匿名 = IP単位の枠)'}")

    print(f"\n  各IPの日次残量(署名は録画に使うIPから出る):")
    rows, total_remaining, warn = [], 0, 0
    own = _fetch_rate_limits(None)
    if own:
        print(f"    {'VPS実IP':26} day {own['day']['remaining']:>3}/{own['day']['max']:<4} "
              f"hour {own['hour']['remaining']:>2}/{own['hour']['max']}  (署名には使わない)")
    try:
        proxies = [l.strip() for l in open(proxies_file, encoding="utf-8") if l.strip() and not l.startswith("#")]
    except OSError:
        proxies = []
    for url in proxies:
        host = url.split("@")[-1]
        j = _fetch_rate_limits(url)
        if not j:
            print(f"    {host:26} 取得失敗")
            continue
        rem, mx = j["day"]["remaining"], j["day"]["max"]
        total_remaining += rem
        pct = rem / mx * 100 if mx else 0
        flag = "  ★残り少" if pct < SIGNATURE_WARN_REMAINING_PCT else ""
        warn += 1 if flag else 0
        print(f"    {host:26} day {rem:>3}/{mx:<4} hour {j['hour']['remaining']:>2}/{j['hour']['max']}"
              f"  使用 {mx-rem}{flag}")
        rows.append({"proxy": host, "day_remaining": rem, "day_max": mx})

    # レート制限の単位を実測から判定する。全IPで残量が完全に一致していれば
    # 共有されている = アカウント単位。1本でもズレていればIPごとに独立。
    # 想定では「APIキーあり = アカウント単位」だが、想定ではなく実測で決める。
    remainings = {r["day_remaining"] for r in rows}
    shared = len(rows) >= 2 and len(remainings) == 1
    scope = "アカウント単位(全IPで共有)" if shared else "IP単位(IPごとに独立)"

    if shared:
        one = rows[0]
        capacity = one["day_max"]
        remaining = one["day_remaining"]
        print(f"\n  レート制限の単位: {scope}")
        print(f"  アカウント全体の日次残量: {remaining} / {capacity}"
              f"  {'OK' if remaining > capacity * SIGNATURE_WARN_REMAINING_PCT / 100 else '★残り少'}")
        if remaining == 0:
            print("  ★★ 枯渇。録画は接続できず、データが取れていない可能性が高い")
    else:
        capacity = sum(r["day_max"] for r in rows)
        remaining = total_remaining
        print(f"\n  レート制限の単位: {scope}")
        print(f"  残り日次署名数の合計: {remaining} / {capacity}"
              f"  {'OK' if warn == 0 else f'★ {warn}本が残り{SIGNATURE_WARN_REMAINING_PCT}%未満'}")
        if remaining == 0 and rows:
            print("  ★★ 全IPで枯渇。録画は接続できず、データが取れていない可能性が高い")

    return {"connects": len(connects), "rate_limited": len(limited),
            "scope": "account" if shared else "per_ip",
            "day_remaining": remaining, "day_capacity": capacity,
            "api_key_set": bool(api_key), "per_ip": rows}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-path", default="data/proxy_pool_trial/proxy5.db")
    p.add_argument("--events-path", default="data/proxy_pool_trial/proxy5_events.jsonl")
    p.add_argument("--log-glob", default="data/proxy_pool_trial/trial*.log")
    p.add_argument("--proxies-file", default="data/proxy_pool_trial/proxy5_ips.txt")
    p.add_argument("--hours", type=float, default=24.0, help="何時間さかのぼるか(既定24)")
    p.add_argument("--json", action="store_true", help="機械可読の1行JSONも出す(通知連携用)")
    args = p.parse_args()

    since = datetime.now(JST) - timedelta(hours=args.hours)
    print(f"TTSLiveTool 健診レポート")
    print(f"対象期間: {since:%m/%d %H:%M} 〜 {datetime.now(JST):%m/%d %H:%M} JST (直近{args.hours:.0f}時間)")

    events = load_events(args.events_path, since)
    if not events:
        print("\n  対象期間にイベントがありません。--hours を広げてください。")
        return 0

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "job": "health_report",
        "window_hours": args.hours,
        "errors": report_error_rates(events),
        "sessions": report_sessions(args.db_path, since),
        "handoffs": report_handoffs(events),
        "signatures": report_signatures(events, args.proxies_file),
    }

    if args.json:
        print()
        print("HEALTH_SUMMARY " + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
