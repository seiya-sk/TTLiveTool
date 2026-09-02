#!/usr/bin/env python3
"""Euler Stream のレート制限が IP単位か アカウント単位か を判定する。

APIキーを設定した状態で、**異なる2つのプロキシIP**から /webcast/rate_limits を
叩く。残量が共有されていればアカウント単位、独立していればIP単位。

匿名(APIキーなし)では2026-09-02 の実測で IP単位だった:
  VPS実IP day 0/100 に対し、10本のプロキシはすべて day 100/100。
APIキーを付けるとアカウント単位に変わるはずだが、それは想定であって
確認していない。想定と違えば設計判断が変わるので、実測で確かめる。

このエンドポイントは情報取得なので、叩いても署名は消費しない。
"""
import argparse
import os
import sys

import httpx

URL = "https://api.eulerstream.com/webcast/rate_limits"
UA = {"User-Agent": "TikTokLive.py/7.0.0"}


def fetch(proxy: str | None, api_key: str | None) -> dict | None:
    headers = dict(UA)
    if api_key:
        headers["X-Api-Key"] = api_key
    try:
        r = httpx.get(URL, proxy=proxy, timeout=20, headers=headers)
        return r.json() if r.status_code == 200 else {"_error": f"HTTP {r.status_code}: {r.text[:120]}"}
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


def line(label: str, j: dict) -> str:
    if not j or "_error" in j:
        return f"  {label:30} {j.get('_error') if j else '取得失敗'}"
    return (f"  {label:30} day {j['day']['remaining']:>4}/{j['day']['max']:<5}"
            f" hour {j['hour']['remaining']:>3}/{j['hour']['max']:<4}"
            f" minute {j['minute']['remaining']}/{j['minute']['max']}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--proxies-file", default="data/proxy_pool_trial/proxy5_ips.txt")
    args = p.parse_args()

    api_key = os.environ.get("SIGN_API_KEY", "").strip()
    print(f"SIGN_API_KEY: {'設定あり(末尾4文字 …' + api_key[-4:] + ')' if api_key else '未設定(匿名)'}")

    proxies = [l.strip() for l in open(args.proxies_file, encoding="utf-8")
               if l.strip() and not l.startswith("#")]
    if len(proxies) < 2:
        print("プロキシが2本以上必要です")
        return 1
    a, b = proxies[0], proxies[1]
    ha, hb = a.split("@")[-1], b.split("@")[-1]

    print("\n=== 現在の残量 ===")
    ja, jb = fetch(a, api_key), fetch(b, api_key)
    print(line(f"IP-A {ha}", ja))
    print(line(f"IP-B {hb}", jb))
    print(line("VPS実IP", fetch(None, api_key)))

    if not ja or not jb or "_error" in ja or "_error" in jb:
        print("\n判定不能(取得に失敗しました)")
        return 1

    same_day = ja["day"]["remaining"] == jb["day"]["remaining"]
    same_max = ja["day"]["max"] == jb["day"]["max"]
    print("\n=== 判定 ===")
    print(f"  2つのIPの day 残量: {ja['day']['remaining']} / {jb['day']['remaining']}"
          f"  → {'一致' if same_day else '不一致'}")
    print(f"  day 上限          : {ja['day']['max']} / {jb['day']['max']}"
          f"  → {'一致' if same_max else '不一致'}")
    print()
    if not api_key:
        print("  APIキーが未設定です。まず SIGN_API_KEY を設定してから再実行してください。")
        print("  (未設定なら匿名扱いで、IP単位になるのが既知の挙動です)")
    elif same_day and same_max:
        print("  → 残量が共有されている = **アカウント単位**(想定どおり)")
        print("     署名のプロキシ経由化はレート制限上は無意味になります。")
        print("     ただしTikTok側から見たIP衛生の観点では引き続き意味があります。")
    else:
        print("  → 残量が独立している = **IP単位のまま**(想定外)")
        print("     APIキーを付けてもIPごとに枠がある、という結果です。")
        print("     署名のプロキシ経由化はレート制限上も有効なままです。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
