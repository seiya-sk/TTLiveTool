#!/usr/bin/env python3
"""ダッシュボードの **クライアント側の挙動** を実ブラウザで確認する(読み取りのみ)。

check_dashboard_nav.py はサーバが返すHTMLしか見ないので、タブ切り替えや
ソートといった JavaScript で動く部分を検証できない。この種の挙動は
UIを触るたびに壊れ得るうえ、壊れても200が返るので気づきにくい。

固定する挙動:
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
import os
import sys

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
