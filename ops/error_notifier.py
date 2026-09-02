#!/usr/bin/env python3
"""系統1: エラー通知(システム異常)。

設計上の性質:
  - 宛先は環境変数 CHATWORK_ERROR_ROOM_ID 固定。UIもDB設定も持たない。
  - **稼働中の録画DBに一切書き込まない**。カーソルと抑止状態は状態ファイル
    (data/notify/error_state.json)に持つ。録画プロセスとのロック競合を
    構造的にゼロにするため。
  - プロデューサ(proxy_pool_trial.py / cleanup_job.py)には手を入れない。
    既に出力されている機械可読ログを追尾するだけなので、録画の再起動が不要。

読むもの:
  data/proxy_pool_trial/proxy5_events.jsonl   1行1JSON(録画側が追記)
  /var/log/tts-cleanup.log の CLEANUP_SUMMARY 行(掃除ジョブが追記)
"""
import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor.notify import chatwork, healthchecks

logger = logging.getLogger("error_notifier")

JST = timezone(timedelta(hours=9))

# --- 通知種別ごとのルール(コード内定数。UI・DBには出さない) -----------
ERROR_RULES = {
    "recording.failed": {"throttle_sec": 600,   "title": "録画失敗"},
    "proxy.degraded":   {"throttle_sec": 1800,  "title": "プロキシ異常"},
    "storage.warning":  {"throttle_sec": 86400, "title": "容量警告"},
    "cleanup.result":   {"throttle_sec": 86400, "title": "掃除結果(日次)"},
}

# 録画失敗として扱う status kind。offline_retry / disconnected / connected は
# 正常な生活環の一部なので含めない(実測で offline_retry だけで27件/50分)。
RECORDING_FAILURE_KINDS = {
    "connection_error",
    "gave_up_repeated_failures",
    "excluded_reconnect_limit",
}

# check_error のうち、プロキシ異常では「ない」もの。
# UserNotFoundError は streamers.txt に残った削除済みアカウント由来で、
# 実測でも check_error の大半(7/8件)を占める。これで警報を出すと
# 恒常的なノイズになるため明示的に除外する。
BENIGN_CHECK_ERRORS = {"UserNotFoundError", "UserOfflineError"}

# WebSocket の正常終了コード。実データ(2026-09-01)で確認したとおり、
# ライブが終わると TikTok 側が code=1000 で接続を正常に閉じてくる:
#   ConnectionClosedError(None, Close(code=<CloseCode.NORMAL_CLOSURE: 1000>, reason=''), None)
# 74分/28分という十分に長い配信の終わりに出ており、直後に recording_ended が
# 続いていた。これを「録画失敗」として通知すると、ライブが終わるたびに誤報が
# 飛ぶ。UserNotFoundError を除外したのと同じ性質の判断。
#
# 1000 以外のクローズコード(1006 異常切断、1011 サーバ内部エラー等)は
# 本物の異常なので、除外せず通知対象のまま残す。
BENIGN_WEBSOCKET_CLOSE_CODES = {1000}

# "code=<CloseCode.NORMAL_CLOSURE: 1000>" と "code=1000" の両方に対応する。
# ライブラリの repr 形式が変わっても数値だけは拾えるようにしておく。
_CLOSE_CODE_RE = re.compile(r"CloseCode\.\w+:\s*(\d+)|code=(\d+)")


def websocket_close_code(error_text: str) -> int | None:
    """ConnectionClosedError の文字列から WebSocket クローズコードを取り出す。
    取り出せなければ None(= 良性と判定せず、通知対象のままにする)。"""
    if "ConnectionClosedError" not in error_text and "ConnectionClosed" not in error_text:
        return None
    m = _CLOSE_CODE_RE.search(error_text)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def is_benign_disconnect(error_text: str) -> bool:
    """ライブ終了に伴う正常な切断か。判定できないものは False を返す
    (迷ったら通知する側に倒す -- 本物の異常を握りつぶさないため)。"""
    code = websocket_close_code(error_text)
    return code is not None and code in BENIGN_WEBSOCKET_CLOSE_CODES


# プロキシ異常とみなす check_error の下限件数(1回のタイムアウトは警報しない)
PROXY_ERROR_MIN_COUNT = 3

DISK_WARN_PCT = 80
CLEANUP_DIGEST_JST_HOUR = 9  # 日次ダイジェストを送る時刻(JST)

DEFAULT_STATE_PATH = "data/notify/error_state.json"


# --- 状態ファイル -------------------------------------------------------
def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 書き込み途中で落ちても壊れない


def tail_new_lines(path: str, cursor: dict) -> tuple[list[str], dict]:
    """カーソル以降の新規行を返す。inode変化・サイズ縮小はローテーションと
    みなして先頭から読み直す。"""
    if not os.path.exists(path):
        return [], cursor
    st = os.stat(path)
    offset = cursor.get("offset", 0)
    if cursor.get("inode") != st.st_ino or st.st_size < offset:
        offset = 0
    if st.st_size == offset:
        return [], {"inode": st.st_ino, "offset": offset}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        data = f.read()
        new_offset = f.tell()
    # 最終行が書き込み途中の可能性があるので、改行で終わっていなければ戻す
    if data and not data.endswith("\n"):
        last_nl = data.rfind("\n")
        if last_nl == -1:
            return [], {"inode": st.st_ino, "offset": offset}
        new_offset = offset + len(data[: last_nl + 1].encode("utf-8"))
        data = data[: last_nl + 1]
    return data.splitlines(), {"inode": st.st_ino, "offset": new_offset}


# --- 収集 ---------------------------------------------------------------
def collect_from_events(lines: list[str]) -> dict:
    """録画イベントJSONLから、録画失敗とプロキシ異常の材料を抜き出す。"""
    failures, proxy_errors, benign_closures = [], [], []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = d.get("event")
        if event == "status" and d.get("kind") in RECORDING_FAILURE_KINDS:
            info = d.get("info") or {}
            error_text = str(info.get("error", ""))
            record = {
                "kind": d.get("kind"),
                "username": d.get("username") or info.get("username"),
                "proxy": d.get("proxy"),
                "error": error_text[:200],
                "timestamp": d.get("timestamp"),
            }
            if is_benign_disconnect(error_text):
                # ライブ終了に伴う正常な切断。通知はしないが、後から
                # 「正常終了が何件あったか」を追えるよう件数は残す。
                benign_closures.append(record)
            else:
                failures.append(record)
        elif event == "check_error":
            cls = str(d.get("error", "")).split("(")[0]
            if cls not in BENIGN_CHECK_ERRORS:
                proxy_errors.append({
                    "error_class": cls,
                    "username": d.get("username"),
                    "proxy": d.get("proxy"),
                    "timestamp": d.get("timestamp"),
                })
        elif event == "status" and d.get("kind") == "signature_rate_limit":
            proxy_errors.append({
                "error_class": "signature_rate_limit",
                "username": d.get("username"),
                "proxy": d.get("proxy"),
                "timestamp": d.get("timestamp"),
            })
    return {
        "recording_failures": failures,
        "proxy_errors": proxy_errors,
        "benign_closures": benign_closures,
    }


def collect_cleanup_summaries(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        idx = line.find("CLEANUP_SUMMARY ")
        if idx == -1:
            continue
        try:
            out.append(json.loads(line[idx + len("CLEANUP_SUMMARY "):]))
        except json.JSONDecodeError:
            continue
    return out


def check_storage(db_path: str) -> dict | None:
    usage = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)) or "/")
    pct = usage.used / usage.total * 100
    if pct < DISK_WARN_PCT:
        return None
    return {
        "disk_pct": round(pct, 1),
        "free_gb": round(usage.free / 1024**3, 1),
        "total_gb": round(usage.total / 1024**3, 1),
        "db_gb": round(os.path.getsize(db_path) / 1024**3, 2) if os.path.exists(db_path) else 0,
    }


# --- 抑止(throttle) ---------------------------------------------------
def throttled(state: dict, key: str, now: float) -> bool:
    last = state.setdefault("throttle", {}).get(key)
    if last is None:
        return False
    return (now - last) < ERROR_RULES[key]["throttle_sec"]


def mark_sent(state: dict, key: str, now: float) -> None:
    state.setdefault("throttle", {})[key] = now
    state.setdefault("suppressed", {}).pop(key, None)


def add_suppressed(state: dict, key: str, n: int) -> None:
    s = state.setdefault("suppressed", {})
    s[key] = s.get(key, 0) + n


# --- メッセージ整形 -----------------------------------------------------
def _jst(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts).astimezone(JST).strftime("%H:%M:%S")
    except ValueError:
        return str(ts)[:19]


def build_recording_failure_message(failures: list[dict], suppressed: int) -> str:
    lines = [f"{len(failures)}件の録画失敗を検知しました。", ""]
    for f in failures[:15]:
        lines.append(f"{_jst(f['timestamp'])} @{f['username']} [{f['kind']}] via {f['proxy']}")
        if f["error"]:
            lines.append(f"  {f['error'][:120]}")
    if len(failures) > 15:
        lines.append(f"... 他 {len(failures) - 15} 件")
    if suppressed:
        lines.append(f"\n(抑止期間中に追加で {suppressed} 件ありました)")
    return chatwork.format_info(f"[録画失敗] {len(failures)}件", "\n".join(lines))


def build_proxy_message(errors: list[dict], suppressed: int) -> str:
    by_class, by_proxy = {}, {}
    for e in errors:
        by_class[e["error_class"]] = by_class.get(e["error_class"], 0) + 1
        by_proxy[e["proxy"]] = by_proxy.get(e["proxy"], 0) + 1
    lines = [f"プロキシ経由のチェックで {len(errors)} 件のエラーが発生しました。", ""]
    lines.append("種別: " + ", ".join(f"{k}×{v}" for k, v in sorted(by_class.items(), key=lambda x: -x[1])))
    lines.append("IP別: " + ", ".join(f"{k}×{v}" for k, v in sorted(by_proxy.items(), key=lambda x: -x[1])[:10]))
    lines.append("\n※ UserNotFoundError(削除済みアカウント)は除外済み")
    if suppressed:
        lines.append(f"(抑止期間中に追加で {suppressed} 件)")
    return chatwork.format_info(f"[プロキシ異常] {len(errors)}件", "\n".join(lines))


def build_storage_message(info: dict) -> str:
    body = (
        f"ディスク使用率が {info['disk_pct']}% に達しました。\n"
        f"空き {info['free_gb']}GB / 全体 {info['total_gb']}GB\n"
        f"録画DB: {info['db_gb']}GB"
    )
    return chatwork.format_info(f"[容量警告] {info['disk_pct']}%", body)


def build_cleanup_digest_message(summaries: list[dict]) -> str:
    runs = len(summaries)
    rows = sum(s.get("rows_deleted", 0) for s in summaries)
    reclaimed = sum(s.get("reclaimed_bytes_est", 0) for s in summaries)
    latest = summaries[-1] if summaries else {}
    body = (
        f"実行 {runs} 回 / 削除 {rows:,} 行 / 回収 {reclaimed / 1048576:.1f}MB\n"
        f"現在のDBサイズ: {latest.get('db_bytes_after', 0) / 1048576:.1f}MB "
        f"(WAL {latest.get('wal_bytes_after', 0) / 1048576:.1f}MB)\n"
        f"保持期間: {latest.get('retention_days', '-')}日\n"
        "\n※ SQLiteの仕様上ファイルサイズは縮まず、解放領域が再利用されます"
    )
    return chatwork.format_info("[掃除結果] 過去24時間", body)


# --- 本体 ---------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-path", default="data/proxy_pool_trial/proxy5_events.jsonl")
    parser.add_argument("--cleanup-log", default="/var/log/tts-cleanup.log")
    parser.add_argument("--db-path", default="data/proxy_pool_trial/proxy5.db")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    parser.add_argument("--dry-run", action="store_true", help="送信せず、送る内容を標準出力に表示")
    parser.add_argument("--heartbeat", action="store_true", help="Healthchecks へのハートビートも行う")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    now = time.time()
    room_id = os.environ.get("CHATWORK_ERROR_ROOM_ID", "")
    if not room_id and not args.dry_run:
        logger.error("CHATWORK_ERROR_ROOM_ID が未設定です")
        return 1

    state = load_state(args.state_path)
    cursors = state.setdefault("cursors", {})

    ev_lines, cursors["events"] = tail_new_lines(args.events_path, cursors.get("events", {}))
    cl_lines, cursors["cleanup"] = tail_new_lines(args.cleanup_log, cursors.get("cleanup", {}))

    collected = collect_from_events(ev_lines)
    summaries = collect_cleanup_summaries(cl_lines)
    state.setdefault("cleanup_buffer", []).extend(summaries)

    outbox = []  # (key, message)

    benign = collected["benign_closures"]
    if benign:
        totals = state.setdefault("benign_totals", {})
        totals["normal_closure"] = totals.get("normal_closure", 0) + len(benign)
        logger.info(
            "正常終了とみなして通知対象から除外: %d件(累計 %d件) -- %s",
            len(benign), totals["normal_closure"],
            ", ".join(f"@{b['username']}" for b in benign[:5]),
        )

    failures = collected["recording_failures"]
    if failures:
        if throttled(state, "recording.failed", now):
            add_suppressed(state, "recording.failed", len(failures))
        else:
            outbox.append(("recording.failed", build_recording_failure_message(
                failures, state.get("suppressed", {}).get("recording.failed", 0))))

    proxy_errors = collected["proxy_errors"]
    if proxy_errors:
        pending = state.get("suppressed", {}).get("proxy.degraded", 0)
        if throttled(state, "proxy.degraded", now):
            add_suppressed(state, "proxy.degraded", len(proxy_errors))
        elif len(proxy_errors) + pending >= PROXY_ERROR_MIN_COUNT:
            outbox.append(("proxy.degraded", build_proxy_message(proxy_errors, pending)))
        else:
            add_suppressed(state, "proxy.degraded", len(proxy_errors))

    storage = check_storage(args.db_path)
    if storage and not throttled(state, "storage.warning", now):
        outbox.append(("storage.warning", build_storage_message(storage)))

    # 掃除結果は日次ダイジェスト。ジョブ自体の失敗は Healthchecks が捕捉する。
    today_jst = datetime.now(JST).strftime("%Y-%m-%d")
    if (
        datetime.now(JST).hour >= CLEANUP_DIGEST_JST_HOUR
        and state.get("last_cleanup_digest_date") != today_jst
        and state.get("cleanup_buffer")
    ):
        outbox.append(("cleanup.result", build_cleanup_digest_message(state["cleanup_buffer"])))

    # --- 送信 ---
    sent_keys = []
    for key, message in outbox:
        if args.dry_run:
            print(f"\n===== [dry-run] {key} =====\n{message}\n")
            sent_keys.append(key)
            continue
        try:
            chatwork.send_message(room_id, message)
            logger.info("送信しました: %s", key)
            sent_keys.append(key)
        except chatwork.ChatworkError as exc:
            logger.error("送信に失敗しました (%s): %s", key, exc)

    for key in sent_keys:
        mark_sent(state, key, now)
        if key == "cleanup.result":
            state["last_cleanup_digest_date"] = today_jst
            state["cleanup_buffer"] = []

    # 掃除バッファは無制限に伸ばさない(48時間分あれば日次には十分)
    if len(state.get("cleanup_buffer", [])) > 96:
        state["cleanup_buffer"] = state["cleanup_buffer"][-96:]

    if not args.dry_run:
        save_state(args.state_path, state)

    logger.info(
        "%s新規イベント %d行 / 録画失敗 %d / プロキシ異常 %d / 正常切断(除外) %d / 送信 %d件",
        "[dry-run] " if args.dry_run else "",
        len(ev_lines), len(failures), len(proxy_errors), len(benign), len(sent_keys),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
