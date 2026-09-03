#!/usr/bin/env python3
"""ダッシュボードの **クライアント側の挙動** を実ブラウザで確認する(読み取りのみ)。

check_dashboard_nav.py はサーバが返すHTMLしか見ないので、タブ切り替えや
ソートといった JavaScript で動く部分を検証できない。この種の挙動は
UIを触るたびに壊れ得るうえ、壊れても200が返るので気づきにくい。

固定する挙動:
  0. 通知設定のCSV往復(エクスポート/インポート)
  1. ライブ一覧の既定タブが「直近のライブ」であること
  2. ソートを切り替えると順位が表示順に追随すること
  3. 「上位ほど良い」ではない並び(昇順・名前順・日時順)では、
     メダル / TOPバッジ / 行の強調を出さないこと
  4. 同値が同順位になり、次の順位が飛ぶこと(1,1,3)

4 は実データに根ざした要件で、新規フォロワーは93%の行が他の行と同値、
うち22行が0だった(2026-09-02 実測)。等しい値に別々の番号を振ると、
意味のない差を見せることになる。

3 は実際に起きていた不具合の再発防止。獲得ダイヤを昇順にすると、
0ダイヤの行が「1位」として金メダルと「TOP」バッジを付けて表示されていた。

使い方:
    python ops/check_dashboard_ui.py [--base http://127.0.0.1:3000]
"""
import argparse
import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_LOCAL = os.path.join(REPO_ROOT, "dashboard", ".env.local")

# 表示中のテーブルだけを対象にする。タブは非表示ペインもDOMに残るため
# (display:none)、単純な querySelector では隠れた表を掴んでしまう。
VISIBLE_TABLE = "[...document.querySelectorAll('table')].find(t => t.offsetParent !== null)"


def load_credentials() -> tuple[str, str] | None:
    """Basic認証の資格情報を読む。**値は表示しない。**"""
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
    return (user, password) if user and password else None


class Check:
    """成否を数えながら1行ずつ報告する。"""

    def __init__(self) -> None:
        self.failures = 0

    def ok(self, condition: bool, label: str, detail: str = "") -> bool:
        if not condition:
            self.failures += 1
        print(f"  {'OK ' if condition else '★NG'} {label}{('  -- ' + detail) if detail else ''}")
        return condition


async def visible_rows(page, limit: int = 30) -> list[dict]:
    """表示中のテーブルの行を、順位・名前・各指標つきで返す。"""
    await page.wait_for_selector("table:visible", timeout=15000)
    return await page.evaluate(
        """(args) => {
            const [sel, limit] = args;
            const tbl = eval(sel);
            if (!tbl) return [];
            const headers = [...tbl.querySelectorAll('thead th')].map(t => t.innerText.trim());
            return [...tbl.querySelectorAll('tbody tr')].slice(0, limit).map(tr => {
                const tds = [...tr.querySelectorAll('td')];
                const text = i => (tds[i] ? tds[i].innerText.trim().split(String.fromCharCode(10)).join(' ') : '');
                const cell = {};
                headers.forEach((h, i) => { cell[h.replace(/[ ▼▲]/g, '')] = text(i); });
                return {
                    rank: parseInt(text(0), 10),
                    name: text(1),
                    cells: cell,
                    medal: !!tr.querySelector('td span[class*="medal"]'),
                    topBadge: tr.innerText.includes('TOP'),
                    highlighted: tr.className.includes('-row-top'),
                };
            });
        }""",
        [VISIBLE_TABLE, limit],
    )


async def sort_indicator(page) -> str | None:
    await page.wait_for_selector("table:visible", timeout=15000)
    return await page.evaluate(
        f"""() => {{
            const tbl = {VISIBLE_TABLE};
            if (!tbl) return null;
            const th = [...tbl.querySelectorAll('thead th')].find(t => /[▼▲]/.test(t.innerText));
            return th ? th.innerText.trim() : null;
        }}"""
    )


async def click_header(page, label: str) -> None:
    await page.evaluate(
        f"""(label) => {{
            const tbl = {VISIBLE_TABLE};
            const th = [...tbl.querySelectorAll('thead th')].find(t => t.innerText.includes(label));
            if (th) th.click();
        }}""",
        label,
    )
    await page.wait_for_timeout(350)


def numeric(value: str) -> float | None:
    digits = "".join(ch for ch in value if ch.isdigit() or ch == "-")
    return float(digits) if digits else None


def check_rank_follows_order(check: Check, rows: list[dict], label: str) -> None:
    """順位が表示順に追随しているか。同順位を許すので、単調非減少で見る。"""
    ranks = [r["rank"] for r in rows if r["rank"] is not None]
    monotonic = all(a <= b for a, b in zip(ranks, ranks[1:]))
    starts_at_one = bool(ranks) and ranks[0] == 1
    check.ok(monotonic and starts_at_one, f"{label}: 順位が表示順に追随",
             f"先頭10件 {ranks[:10]}")


def check_ties_share_rank(check: Check, rows: list[dict], column: str) -> None:
    """同値が同順位になり、次の順位が飛ぶこと(1,1,3)。"""
    groups: dict[float, set[int]] = {}
    for r in rows:
        value = numeric(r["cells"].get(column, ""))
        if value is None or r["rank"] is None:
            continue
        groups.setdefault(value, set()).add(r["rank"])
    tied = {v: rs for v, rs in groups.items() if len([r for r in rows
            if numeric(r["cells"].get(column, "")) == v]) > 1}
    if not tied:
        print(f"  --  同値の行が無く判定不能({column})")
        return
    same_rank = all(len(rs) == 1 for rs in tied.values())
    check.ok(same_rank, f"同値は同順位({column})",
             f"同値グループ {len(tied)}件")

    # 次の順位が飛ぶこと: 同順位が n 件あれば、次の順位は n だけ進む
    ordered = [r for r in rows if r["rank"] is not None]
    skipped_ok = True
    detail = ""
    for i in range(1, len(ordered)):
        prev_rank = ordered[i - 1]["rank"]
        rank = ordered[i]["rank"]
        if rank == prev_rank:
            continue
        expected = i + 1
        if rank != expected:
            skipped_ok = False
            detail = f"{i}行目で順位{rank}(期待 {expected})"
            break
    check.ok(skipped_ok, "同順位のぶん次の順位が飛ぶ", detail)


def check_no_decoration(check: Check, rows: list[dict], label: str) -> None:
    """「上位ほど良い」ではない並びで装飾が出ていないこと。"""
    medals = sum(1 for r in rows if r["medal"])
    tops = sum(1 for r in rows if r["topBadge"])
    highlights = sum(1 for r in rows if r["highlighted"])
    check.ok(medals == 0 and tops == 0 and highlights == 0,
             f"{label}: メダル/TOP/行強調を出さない",
             f"メダル{medals} TOP{tops} 強調{highlights}")


def check_decoration_present(check: Check, rows: list[dict], label: str) -> None:
    """降順のランキングでは、ちゃんと装飾が出ること(消しすぎの検出)。"""
    medals = sum(1 for r in rows if r["medal"])
    tops = sum(1 for r in rows if r["topBadge"])
    check.ok(medals > 0 and tops > 0, f"{label}: 1位に装飾が出る",
             f"メダル{medals} TOP{tops}")


# --- 通知設定のCSV往復 ------------------------------------------------------
# 通知設定は「静かに壊れる」機能で、壊れても画面はエラーを出さず、
# 進捗通知が届かなくなって初めて気づく。CSV は特に、検証が1つ緩んだだけで
# 「設定したつもりが反映されていない」状態になる。
#
# **本番の割り当てを変更しない。** 検査専用のグループを作り、その列だけを
# 対象にした CSV で試す(CSVに列が無いグループは変更されない、という仕様
# そのものを利用する)。グループは finally で必ず消す -- 実行後に元へ戻す
# 方式だと、途中で落ちたときに汚れが残る。
CHECK_GROUP_PREFIX = "__UI検査__"


def _db_path() -> str:
    return os.path.join(REPO_ROOT, "data", "proxy_pool_trial", "proxy5.db")


def _assignment_count() -> int:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM notification_group_streamers").fetchone()[0]
    finally:
        conn.close()


def _assignments_excluding(group_id: int) -> set:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    try:
        return {
            (g, s) for g, s in conn.execute(
                "SELECT group_id, streamer_id FROM notification_group_streamers WHERE group_id != ?",
                (group_id,),
            )
        }
    finally:
        conn.close()


def _auth_header() -> dict:
    creds = load_credentials()
    if not creds:
        return {}
    token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _request(base: str, path: str, method: str = "GET",
             payload: dict | None = None) -> tuple[int, object]:
    """APIを叩いて (HTTPコード, 本文) を返す。本文はJSONなら辞書、でなければ文字列。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = _auth_header()
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            raw = res.read().decode("utf-8")
            status = res.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        status = e.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _post_csv(base: str, csv_text: str, apply: bool) -> tuple[int, dict]:
    status, body = _request(base, "/api/notifications/assignments", "POST",
                            {"csv": csv_text, "apply": apply})
    return status, body if isinstance(body, dict) else {}


def _create_check_group(base: str) -> int | None:
    name = f"{CHECK_GROUP_PREFIX}{os.getpid()}"
    status, groups = _request(base, "/api/notifications/groups", "POST",
                              {"name": name, "roomId": "0"})
    if status != 200 or not isinstance(groups, list):
        return None
    for g in groups:
        if g.get("name") == name:
            return g.get("id")
    return None


def _delete_group(base: str, group_id: int) -> None:
    _request(base, f"/api/notifications/groups/{group_id}", "DELETE")


async def check_csv_transfer(base: str, page, check: Check) -> None:
    print("\n--- 0. 通知設定のCSV往復 ---")
    group_id = _create_check_group(base)
    if group_id is None:
        check.ok(False, "検査用グループを作成できない")
        return

    tmpdir = tempfile.mkdtemp(prefix="ui-check-csv-")
    try:
        code, export_body = _request(base, "/api/notifications/assignments")
        check.ok(code == 200 and isinstance(export_body, str), "CSVをエクスポートできる", f"HTTP {code}")
        check.ok(export_body.startswith("\ufeff"), "BOM付き(Excelで文字化けしない)")

        header = export_body.lstrip("\ufeff").splitlines()[0]
        heading = f"[#{group_id}]"
        check.ok(heading in header, "見出しにグループIDが入っている", header[:80])

        # (1) 往復の安定性。**apply ではなく preview で確かめる。**
        # 差分が出ないことを先に確認してから書き込む方が安全で、
        # 万一バグがあっても本番の割り当てを壊さずに検出できる。
        status, body = _post_csv(base, export_body, apply=False)
        ok = status == 200 and not body.get("added") and not body.get("removed")
        check.ok(ok, "エクスポートしたCSVをそのまま入れると差分0",
                 f"HTTP {status} 追加{len(body.get('added', []))}/削除{len(body.get('removed', []))}")

        # (2) 存在しない username は 422 で拒否し、DBを変えない
        before = _assignment_count()
        status, body = _post_csv(
            base, f"username,表示名,検査 [#{group_id}]\n__no_such_user__,,1\n", apply=True)
        check.ok(status == 422, "存在しないusernameを422で拒否", f"HTTP {status}")
        check.ok(any("登録されていません" in e for e in body.get("errors", [])),
                 "拒否の理由を返す", str(body.get("errors"))[:60])
        check.ok(_assignment_count() == before, "拒否時にDBが変わっていない")

        # (3) 検査用グループの列だけを持つCSVを適用し、他グループが保護されるか
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
        target = conn.execute(
            "SELECT tiktok_account_id FROM streamers WHERE archived = 0 ORDER BY id LIMIT 1"
        ).fetchone()
        conn.close()
        others_before = _assignments_excluding(group_id)
        scoped = f"username,表示名,検査 [#{group_id}]\n{target[0]},,1\n"
        status, body = _post_csv(base, scoped, apply=True)
        check.ok(status == 200 and len(body.get("added", [])) == 1,
                 "検査用グループへの割り当てを適用できる", f"HTTP {status}")
        check.ok(_assignments_excluding(group_id) == others_before,
                 "CSVに列が無いグループの割り当てが保護される")
        check.ok(any("含まれていない" in w for w in body.get("warnings", [])),
                 "CSVに無いライバーを警告する")

        # (4) 差分プレビューが件数だけでなく一覧を出すこと(画面で確認)
        csv_path = os.path.join(tmpdir, "check.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("\ufeff" + f"username,表示名,検査 [#{group_id}]\n{target[0]},,0\n")
        await page.goto(base + "/settings/notifications", wait_until="networkidle")
        await page.set_input_files("input[type=file]", csv_path)
        await page.wait_for_timeout(2500)
        # **差分の領域に限って見る。** ページ全体を対象にすると、同じ名前が
        # グループのメンバー一覧など他の場所にも出ているため、差分の一覧を
        # 消しても検査が通ってしまう(実際に退行を注入して確認した)。
        diff_text = await page.evaluate(
            """() => {
                const head = [...document.querySelectorAll('*')]
                  .find(e => e.textContent.trim() === 'CSVで一括編集');
                if (!head) return '';
                const card = head.parentElement;
                const lists = [...card.querySelectorAll('ul')];
                return lists.map(u => u.innerText).join('\\n');
            }"""
        )
        card_text = await page.evaluate(
            """() => {
                const head = [...document.querySelectorAll('*')]
                  .find(e => e.textContent.trim() === 'CSVで一括編集');
                return head ? head.parentElement.innerText : '';
            }"""
        )
        check.ok("削除される" in card_text, "差分プレビューが表示される")
        check.ok(target[0] in diff_text, "誰が対象かを差分の一覧に出す(件数だけでない)",
                 f"@{target[0]} が一覧に含まれるか")
        check.ok("この内容で反映する" in card_text, "確認してから反映するボタンがある")
    finally:
        # **必ず片付ける。** グループを消せば割り当ても CASCADE で消える。
        _delete_group(base, group_id)
        for name in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, name))
        os.rmdir(tmpdir)
        conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
        left = conn.execute(
            "SELECT COUNT(*) FROM notification_groups WHERE name LIKE ?",
            (CHECK_GROUP_PREFIX + "%",)).fetchone()[0]
        conn.close()
        check.ok(left == 0, "検査用グループを削除した", f"残り {left}件")
        check.ok(not os.path.exists(tmpdir), "一時CSVを削除した")


async def run(base: str) -> int:
    from playwright.async_api import async_playwright

    check = Check()
    creds = load_credentials()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            http_credentials=({"username": creds[0], "password": creds[1]} if creds else None),
            viewport={"width": 1500, "height": 950},
        )
        page = await context.new_page()

        await check_csv_transfer(base, page, check)

        # --- 1. ホームからの導線と既定タブ ---
        print("\n--- 1. ホーム『直近のライブ』→ ライブ一覧の既定タブ ---")
        await page.goto(base + "/", wait_until="networkidle")
        heading = await page.evaluate(
            "() => { const h = document.querySelector('.home-panel-title'); return h ? h.innerText.trim() : null; }"
        )
        check.ok(heading == "直近のライブ", "ホームの見出しが『直近のライブ』", f"実際: {heading}")
        home_names = await page.evaluate(
            """() => [...document.querySelectorAll('.recent-sessions-table tbody tr')].slice(0,5)
                 .map(r => r.querySelector('td').innerText.trim().split(String.fromCharCode(10)).join(' '))"""
        )

        await page.click(".home-panel-link")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_selector(".tab-button.active", timeout=15000)
        active_tab = await page.evaluate(
            "() => { const b = document.querySelector('.tab-button.active'); return b ? b.innerText.trim() : null; }"
        )
        check.ok(active_tab == "直近のライブ", "既定タブが『直近のライブ』", f"実際: {active_tab}")

        tabs = await page.evaluate(
            "() => [...document.querySelectorAll('.tab-button')].map(b => b.innerText.trim())")
        check.ok(tabs[0] == "直近のライブ" and "ダイヤランキング" in tabs,
                 "既存タブが後ろに残っている", f"{tabs}")

        rows = await visible_rows(page, 5)
        list_names = [r["name"] for r in rows]
        check.ok(
            [n.split(" ")[0] for n in home_names] == [n.split(" ")[0] for n in list_names],
            "ホームと同じ並び(同じクエリ・同じ順序)",
            f"ホーム {home_names[:3]} / 一覧 {list_names[:3]}",
        )
        check_no_decoration(check, await visible_rows(page), "直近のライブ(日時順)")

        # --- 2. 降順ランキングでの順位と装飾 ---
        print("\n--- 2. 降順ランキング ---")
        for tab_label, column in [("ダイヤランキング", "獲得ダイヤ"),
                                  ("新規フォロワーランキング", "新規フォロワー"),
                                  ("同接ランキング", "最高同接")]:
            await page.click(f".tab-button:has-text('{tab_label}')")
            await page.wait_for_timeout(350)
            rows = await visible_rows(page)
            indicator = await sort_indicator(page)
            check.ok(indicator is not None and "▼" in indicator,
                     f"{tab_label}: 降順で開く", f"{indicator}")
            check_rank_follows_order(check, rows, tab_label)
            check_decoration_present(check, rows, tab_label)
            check_ties_share_rank(check, rows, column)

        # --- 3. 昇順にすると装飾が消えること ---
        print("\n--- 3. 昇順(上位ほど良い並びではない)---")
        await page.click(".tab-button:has-text('ダイヤランキング')")
        await page.wait_for_timeout(300)
        await click_header(page, "獲得ダイヤ")          # 降順 -> 昇順
        indicator = await sort_indicator(page)
        check.ok(indicator is not None and "▲" in indicator, "昇順に切り替わる", f"{indicator}")
        rows = await visible_rows(page)
        check_rank_follows_order(check, rows, "獲得ダイヤ昇順")
        check_no_decoration(check, rows, "獲得ダイヤ昇順")
        check_ties_share_rank(check, rows, "獲得ダイヤ")

        # --- 4. 名前順(数値ではない並び)---
        print("\n--- 4. 名前順(数値ではない並び)---")
        await page.click(".tab-button:has-text('同接ランキング')")
        await page.wait_for_timeout(300)
        await click_header(page, "ライバー")
        rows = await visible_rows(page)
        check_rank_follows_order(check, rows, "ライバー名順")
        check_no_decoration(check, rows, "ライバー名順")

        # --- 5. ライバー一覧でも同じ扱いか ---
        print("\n--- 5. ライバー一覧 ---")
        await page.goto(base + "/streamers", wait_until="networkidle")
        rows = await visible_rows(page)
        check_rank_follows_order(check, rows, "ライバー一覧(既定)")
        header_label = await page.evaluate(
            f"""() => {{
                const tbl = {VISIBLE_TABLE};
                const th = [...tbl.querySelectorAll('thead th')].find(t => /[▼▲]/.test(t.innerText));
                return th ? th.innerText.replace(/[ ▼▲]/g, '') : null;
            }}"""
        )
        if header_label:
            await click_header(page, header_label)   # 降順 -> 昇順
            rows = await visible_rows(page)
            check_no_decoration(check, rows, "ライバー一覧(昇順)")
        else:
            print("  --  既定の並べ替えが無く判定不能")

        await browser.close()

    print()
    print("すべて問題なし" if check.failures == 0 else f"★ {check.failures}件の問題")
    return 1 if check.failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="http://127.0.0.1:3000")
    args = p.parse_args()
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("playwright が入っていません: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 1
    return asyncio.run(run(args.base.rstrip("/")))


if __name__ == "__main__":
    raise SystemExit(main())
