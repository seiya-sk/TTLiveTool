#!/usr/bin/env python3
"""録画プロセスの死活監視を Healthchecks.io へ送る。

無条件に ping を打つと「timer が生きている」ことしか証明できず、録画が
死んでいても正常扱いになる。そこで健全性を判定してから打ち分ける:

  録画プロセスが生存 かつ events.jsonl が直近 N 分以内に更新されている
    -> ping()  (正常)
  いずれか欠けている
    -> fail()  (Healthchecks 側が即座に警報)

events.jsonl の更新を条件に入れているのは、プロセスだけ生きて実質何も
していない(ハング)状態を検知するため。check イベントは配信の有無に
関わらず 5 秒間隔で出続けるので、数分の無更新は異常を意味する。

VPS ごと落ちた場合は ping が途絶えるので、Healthchecks 側のタイムアウト
で検知される。Chatwork 通知は VPS が動いていないと送れないため、この
二重化に意味がある。
"""
import argparse
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tiktok_monitor.notify import healthchecks

logger = logging.getLogger("recorder_heartbeat")

UUID_ENV = "HEALTHCHECKS_RECORDER_UUID"


def process_alive(pattern: str) -> bool:
    """pgrep -f は自分自身のコマンドラインにもマッチする -- このスクリプトを
    --process-pattern 付きで起動すると、引数に含まれるパターン文字列が
    自分にヒットして「常に生存」という致命的な誤判定になる。自PIDと、
    このスクリプト自身のプロセスを除外してから判定する。"""
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    self_pid = os.getpid()
    own_marker = os.path.basename(__file__)
    for raw in result.stdout.split():
        try:
            pid = int(raw)
        except ValueError:
            continue
        if pid == self_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", "replace")
        except OSError:
            continue  # 読む前に終了したプロセス
        if own_marker in cmdline:
            continue
        return True
    return False


def events_fresh(path: str, max_age_sec: float) -> tuple[bool, float | None]:
    if not os.path.exists(path):
        return False, None
    age = time.time() - os.path.getmtime(path)
    return age <= max_age_sec, age


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-path", default="data/proxy_pool_trial/proxy5_events.jsonl")
    parser.add_argument("--process-pattern", default="tiktok_monitor.proxy_pool_trial")
    parser.add_argument("--max-age-sec", type=float, default=600)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    alive = process_alive(args.process_pattern)
    fresh, age = events_fresh(args.events_path, args.max_age_sec)
    healthy = alive and fresh

    age_text = f"{age:.0f}秒前" if age is not None else "ファイルなし"
    detail = f"process={'alive' if alive else 'DOWN'} events_updated={age_text}"

    if args.dry_run:
        print(f"[dry-run] {'ping' if healthy else 'FAIL'}: {detail}")
        return 0

    uuid = os.environ.get(UUID_ENV, "")
    if not uuid:
        logger.info("%s が未設定のためスキップ (%s)", UUID_ENV, detail)
        return 0

    if healthy:
        healthchecks.ping(uuid, payload=detail)
        logger.info("ping: %s", detail)
    else:
        healthchecks.fail(uuid, payload=detail)
        logger.warning("fail: %s", detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
