#!/usr/bin/env python3
"""テーブル別のディスク使用量を集計してJSONに書き出す(読み取り専用)。

なぜ別プロセスなのか
--------------------
ダッシュボードは better-sqlite3 を使っており、これは **同期API** である。
`SELECT name, SUM(pgsize) FROM dbstat GROUP BY name` は DB の全ページを
走査するので、4.6GB になった時点で 39秒かかる。それを Next.js の
リクエスト処理の中で実行すると、Node のイベントループが39秒間止まり、
**ダッシュボード全体が固まる**。

実測(2026-09-02): /settings/data の処理中に別のページ(/)を開いたところ、
通常0.7秒のところ 28.3秒かかった。データ管理を開いた人だけでなく、
同時に見ている全員が巻き添えになる。

そこでこのスクリプトを別プロセスで走らせ、結果をJSONに落とす。
ダッシュボードはそのJSONを読むだけなので即座に応答できる。集計中も
古い値を返せるため、画面が固まることはない。

使い方:
    python ops/table_sizes.py --db-path data/proxy_pool_trial/proxy5.db \
        --out data/table_sizes.json
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone


def collect(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        t0 = time.time()
        tables = {
            name: int(size or 0)
            for name, size in conn.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"
            )
        }
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        elapsed = time.time() - t0
    finally:
        conn.close()
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "db_path": os.path.abspath(db_path),
        "db_bytes": os.path.getsize(db_path),
        "free_bytes": int(freelist) * int(page_size),
        "elapsed_sec": round(elapsed, 2),
        "tables": tables,
    }


def write_atomic(path: str, payload: dict) -> None:
    """一時ファイルに書いてから rename する。読み手が途中の状態を
    掴まないようにするため(ダッシュボードは任意のタイミングで読む)。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default="data/proxy_pool_trial/proxy5.db")
    p.add_argument("--out", default="data/table_sizes.json")
    args = p.parse_args()

    if not os.path.exists(args.db_path):
        print(f"DBが見つかりません: {args.db_path}", file=sys.stderr)
        return 1

    payload = collect(args.db_path)
    write_atomic(args.out, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
