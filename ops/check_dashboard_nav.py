#!/usr/bin/env python3
"""ダッシュボードの導線を一通り確認する(読み取りのみ)。

確認するもの:
  1. 「戻る」リンクの遷移先が、遷移元に応じて正しく変わるか
     -- 不正な from を弾いて既定へ落ちることも含む(外部URLを差し込めない)
  2. 内部リンクを辿って 200 以外が出ないか
  3. 画像API(アバター/スクリーンショット)が生きているか
  4. 各ページの応答時間

ダッシュボードにJSのテスト基盤が無いため、代わりに実際に起動している
サーバへHTTPで当てて確認する。手で確認するより取りこぼしが少なく、
UIを触ったあとに毎回流せる。

使い方:
    python ops/check_dashboard_nav.py [--base http://127.0.0.1:3000]
"""
import argparse
import base64
import os
import re
import subprocess
import sys
import time
from collections import deque

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_LOCAL = os.path.join(REPO_ROOT, "dashboard", ".env.local")


def load_auth() -> str | None:
    """Basic認証のヘッダを組み立てる。**値は表示しない。**"""
    user = password = None
    try:
        with open(ENV_LOCAL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DASHBOARD_BASIC_AUTH_USER="):
                    user = line.split("=", 1)[1].strip().strip("\"'")
                elif line.startswith("DASHBOARD_BASIC_AUTH_PASS="):
                    password = line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        return None
    if not user or not password:
        return None
    return base64.b64encode(f"{user}:{password}".encode()).decode()


class Client:
    def __init__(self, base: str, auth: str | None):
        self.base = base.rstrip("/")
        self.headers = ["-H", f"Authorization: Basic {auth}"] if auth else []

    def get(self, path: str) -> tuple[str, str, float]:
        t0 = time.time()
        r = subprocess.run(
            ["curl", "-s", "-w", "\n%{http_code}", *self.headers, self.base + path],
            capture_output=True,
        )
        elapsed = time.time() - t0
        out = r.stdout.decode("utf-8", errors="replace")
        body, _, code = out.rpartition("\n")
        return code.strip(), body, elapsed


BACK_LINK_RE = re.compile(r'<a[^>]*class="back-link"[^>]*>(.*?)</a>', re.S)
HREF_RE = re.compile(r'href="([^"]*)"')


def back_link(html: str) -> tuple[str, str]:
    m = BACK_LINK_RE.search(html)
    if not m:
        return "(なし)", "(なし)"
    tag = html[m.start():m.start() + (m.group(0).find(">") + 1)]
    href = HREF_RE.search(tag)
    label = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return (href.group(1) if href else "(なし)"), label


def pick_session_id(client: Client) -> int | None:
    code, body, _ = client.get("/sessions")
    if code != "200":
        return None
    ids = re.findall(r'"/sessions/(\d+)', body) or re.findall(r"/sessions/(\d+)\?", body)
    return int(ids[0]) if ids else None


def check_back_links(client: Client, session_id: int) -> int:
    print(f"\n--- 1. 戻るリンク(/sessions/{session_id}) ---")
    cases = [
        ("遷移元なし(直接URL)", "", "/sessions"),
        ("ライバー詳細から", "?from=streamer", "/streamers/"),
        ("絞り込み一覧から", "?from=filtered", "/sessions?streamerId="),
        ("不正値", "?from=../../evil", "/sessions"),
        ("外部URL", "?from=https%3A%2F%2Fexample.com", "/sessions"),
    ]
    failures = 0
    for name, query, expect_prefix in cases:
        code, body, _ = client.get(f"/sessions/{session_id}{query}")
        href, label = back_link(body)
        ok = code == "200" and href.startswith(expect_prefix)
        if not ok:
            failures += 1
        print(f"  {'OK ' if ok else '★NG'} {name:<22} -> {href:<30} 「{label}」")
    return failures


def crawl(client: Client, limit: int = 120) -> int:
    print("\n--- 2. 内部リンクの巡回 ---")
    seeds = ["/", "/sessions", "/streamers", "/settings/data", "/settings/streamers",
             "/settings/notifications", "/settings/tokens"]
    seen: set[str] = set()
    queue = deque(seeds)
    bad = []
    while queue and len(seen) < limit:
        path = queue.popleft()
        if path in seen:
            continue
        seen.add(path)
        code, body, _ = client.get(path)
        if code != "200":
            bad.append((code, path))
            continue
        for href in set(re.findall(r'href="(/[^"#]*)"', body)):
            if href.startswith(("/api/", "/_next")) or re.search(r"\.(png|jpg|jpeg|svg|ico|css|js)$", href):
                continue
            if href not in seen:
                queue.append(href)
    print(f"  巡回 {len(seen)}ページ / 200以外 {len(bad)}件")
    for code, path in bad:
        print(f"    ★NG {code} {path}")
    return len(bad)


def _screenshot_name() -> str | None:
    """スクリーンショットのファイル名をDBから1つ取る。

    画面のHTMLからは拾えない -- 画像はクライアント側の CompositeChart が
    クリック後に描画するので、サーバが返すHTMLには含まれない。
    APIそのものを叩きたいので、参照元をDBから取る。
    """
    import sqlite3
    db = os.path.join(REPO_ROOT, "data", "proxy_pool_trial", "proxy5.db")
    if not os.path.exists(db):
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT image_path FROM live_screenshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
    except Exception:
        return None
    return os.path.basename(row[0].replace("\\", "/")) if row else None


def check_media(client: Client, session_id: int | None = None) -> int:
    print("\n--- 3. 画像API ---")
    failures = 0

    _, body, _ = client.get("/streamers")
    hits = re.findall(r'"(/api/avatars/[^"]+)"', body)
    if hits:
        code, _, _ = client.get(hits[0].replace("&amp;", "&"))
        failures += 0 if code == "200" else 1
        print(f"  {'OK ' if code == '200' else '★NG'} アバター: {code}")
    else:
        print("  --  アバター: 参照が見つからず(データ無し。判定不能)")

    name = _screenshot_name()
    if name:
        code, _, _ = client.get(f"/api/screenshots/{name}")
        failures += 0 if code == "200" else 1
        print(f"  {'OK ' if code == '200' else '★NG'} スクリーンショット: {code}")
    else:
        print("  --  スクリーンショット: 記録が無く判定不能")
    return failures


def check_timing(client: Client, session_id: int) -> None:
    print("\n--- 4. 応答時間 ---")
    _, body, _ = client.get("/streamers")
    accounts = re.findall(r'"/streamers/([^"/]+)"', body)
    paths = ["/", "/sessions", "/streamers", f"/sessions/{session_id}",
             "/settings/data", "/settings/streamers", "/settings/notifications", "/settings/tokens"]
    if accounts:
        paths.insert(3, f"/streamers/{accounts[0]}")
    for path in paths:
        code, _, elapsed = client.get(path)
        flag = "  ★2秒超" if elapsed > 2.0 else ""
        print(f"  {elapsed:6.2f}秒  {code}  {path}{flag}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="http://127.0.0.1:3000")
    args = p.parse_args()

    client = Client(args.base, load_auth())
    code, _, _ = client.get("/")
    if code == "401":
        print("認証に失敗しました。dashboard/.env.local を確認してください。", file=sys.stderr)
        return 1
    if code != "200":
        print(f"ダッシュボードに接続できません({code})。", file=sys.stderr)
        return 1

    session_id = pick_session_id(client)
    if session_id is None:
        print("セッションが1件も無いため、戻るリンクの確認をスキップします。")
        failures = crawl(client) + check_media(client)
        return 1 if failures else 0

    failures = check_back_links(client, session_id)
    failures += crawl(client)
    failures += check_media(client, session_id)
    check_timing(client, session_id)

    print()
    print("すべて問題なし" if failures == 0 else f"★ {failures}件の問題")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
