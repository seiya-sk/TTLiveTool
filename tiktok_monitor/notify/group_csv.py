#!/usr/bin/env python3
"""通知グループの割り当てを CSV で往復させる。

事務所規模(100人超)の初期設定や大規模な入れ替えを、画面のチェックボックス
ではなく表計算ソフトで行えるようにするためのもの。日常の小さな変更は
UI の一括操作(検索・全選択)で足りる。

**CSV の解析は標準ライブラリの csv に任せる。** グループ名にカンマ・改行・
引用符が入りうるので、split(",") では壊れる。書き出しも同じ理由で
csv.writer を通す。

【列の同定について】
見出しは `表示名 [#ID]` の形にする。グループ名だけを見出しにすると、
グループ名を変えた時点で手元の CSV が使えなくなる。ID を併記しておけば
名前が変わっても同定できる。読み込み側は次の順で解釈する:
    1. `[#12]` が付いていれば ID で同定(名前が違っても ID を優先)
    2. 付いていなければ名前で同定(手書きの CSV も受け付ける)
名前と ID が食い違う場合は ID を採用し、警告として報告する。

【行の同定について】
username には streamers.tiktok_account_id を使う。UNIQUE NOT NULL 制約が
あり、行を一意に指せる唯一の列。表示名(streamers.name)は NULL 可で
一意制約も無いため、キーには使えない。

使い方(いずれも --db-path 必須):
    export   … 現在の割り当てを CSV で標準出力へ
    preview  … 標準入力の CSV を検証し、差分を JSON で出す(DBは変更しない)
    apply    … 標準入力の CSV を検証して適用し、結果を JSON で出す
"""
import argparse
import csv
import io
import json
import re
import sys

from .. import db

# Excel は BOM が無いと UTF-8 の日本語を Shift_JIS と誤認して文字化けする。
# BOM 付きで書き出し、読み込み時は utf-8-sig で剥がす(BOM が無いファイルも
# そのまま読める)。
BOM = "﻿"

USERNAME_COLUMN = "username"
DISPLAY_NAME_COLUMN = "表示名"

# 見出しから `[#12]` を取り出す。名前側に `[` が含まれていても、末尾の
# `[#数字]` だけを見るので誤爆しない。
_GROUP_ID_RE = re.compile(r"^(?P<name>.*?)\s*\[#(?P<id>\d+)\]\s*$")


def group_heading(group_id: int, name: str) -> str:
    return f"{name} [#{group_id}]"


def parse_group_heading(heading: str) -> tuple[str, int | None]:
    """見出しを (名前, ID) に分解する。ID が無ければ None。"""
    m = _GROUP_ID_RE.match(heading.strip())
    if m:
        return m.group("name").strip(), int(m.group("id"))
    return heading.strip(), None


def _groups(conn):
    return conn.execute(
        "SELECT id, name FROM notification_groups ORDER BY id"
    ).fetchall()


def _streamers(conn):
    # アーカイブ済みは通知対象外なので出さない。UI のピッカーと同じ母集団に
    # 揃えないと、CSV で往復したときに勝手に増減して見える。
    return conn.execute(
        "SELECT id, tiktok_account_id, COALESCE(name, '') FROM streamers "
        "WHERE archived = 0 ORDER BY tiktok_account_id"
    ).fetchall()


def _assignments(conn) -> set[tuple[int, int]]:
    return {
        (g, s) for g, s in conn.execute(
            "SELECT group_id, streamer_id FROM notification_group_streamers"
        )
    }


def export_csv(conn) -> str:
    """行=ライバー、列=グループ。未割り当てのライバーも必ず含める。

    含めるのは、設定漏れを見つけるため。全部 0 の行がそのまま
    「どのグループにも入っていない人」の一覧になる。
    """
    groups = _groups(conn)
    streamers = _streamers(conn)
    assigned = _assignments(conn)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        [USERNAME_COLUMN, DISPLAY_NAME_COLUMN]
        + [group_heading(gid, name) for gid, name in groups]
    )
    for sid, account, name in streamers:
        writer.writerow(
            [account, name] + [1 if (gid, sid) in assigned else 0 for gid, _ in groups]
        )
    return BOM + out.getvalue()


class CsvError(Exception):
    """検証に落ちた。**何も変更していない。** 複数の理由をまとめて返す。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _read_rows(text: str) -> tuple[list[str], list[dict]]:
    reader = csv.reader(io.StringIO(text.lstrip(BOM)))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvError(["CSVが空です。"])
    header = [h.strip() for h in header]
    rows = []
    for line_no, values in enumerate(reader, start=2):
        if not any(v.strip() for v in values):
            continue        # 空行は読み飛ばす(表計算ソフトが末尾に付けがち)
        rows.append({"line": line_no, "values": values})
    return header, rows


def analyze(conn, text: str) -> dict:
    """検証して差分を返す。**DBは一切変更しない。**

    エラーは1件見つけても即座に返さず、全部集めてから投げる。1行ずつ直して
    再アップロードを繰り返すのは、100人規模だと現実的でない。
    """
    groups = _groups(conn)
    by_id = {gid: name for gid, name in groups}
    by_name = {name: gid for gid, name in groups}
    streamers = _streamers(conn)
    by_account = {acc: (sid, name) for sid, acc, name in streamers}

    header, rows = _read_rows(text)
    errors: list[str] = []
    warnings: list[str] = []

    if not header or header[0].strip().lower() != USERNAME_COLUMN:
        errors.append(
            f"1列目が「{USERNAME_COLUMN}」ではありません。"
            f"(実際: 「{header[0] if header else ''}」)"
        )
        raise CsvError(errors)

    # --- 列(グループ)の同定 ---
    group_columns: list[tuple[int, int]] = []      # (列位置, group_id)
    for idx, heading in enumerate(header):
        if idx == 0 or heading == DISPLAY_NAME_COLUMN:
            continue
        if not heading:
            continue
        name, gid = parse_group_heading(heading)
        if gid is not None:
            if gid not in by_id:
                errors.append(f"列「{heading}」: ID {gid} のグループが存在しません。")
                continue
            if name and name != by_id[gid]:
                warnings.append(
                    f"列「{heading}」: 名前が現在の「{by_id[gid]}」と違います。"
                    f"IDを優先して扱います。"
                )
            group_columns.append((idx, gid))
        else:
            if name not in by_name:
                errors.append(f"列「{heading}」: そのグループは存在しません。")
                continue
            group_columns.append((idx, by_name[name]))

    if not group_columns and not errors:
        errors.append("グループの列が1つもありません。")

    # --- 行の検証 ---
    desired: set[tuple[int, int]] = set()
    seen_accounts: set[str] = set()
    for row in rows:
        values = row["values"]
        account = (values[0] if values else "").strip()
        if not account:
            errors.append(f"{row['line']}行目: username が空です。")
            continue
        if account not in by_account:
            errors.append(f"{row['line']}行目: username「{account}」は登録されていません。")
            continue
        if account in seen_accounts:
            errors.append(f"{row['line']}行目: username「{account}」が重複しています。")
            continue
        seen_accounts.add(account)
        sid = by_account[account][0]
        for idx, gid in group_columns:
            raw = (values[idx] if idx < len(values) else "").strip()
            if raw in ("1", "0"):
                if raw == "1":
                    desired.add((gid, sid))
            elif raw == "":
                # 空欄は 0 と同じに扱う。表計算ソフトは 0 を空欄にしがちで、
                # ここでエラーにすると実用に耐えない。
                continue
            else:
                errors.append(
                    f"{row['line']}行目「{header[idx]}」: 値は 1 か 0 のみです。"
                    f"(実際: 「{raw}」)"
                )

    if errors:
        raise CsvError(errors)

    # --- CSV に無いライバー ---
    missing = [acc for _, acc, _ in streamers if acc not in seen_accounts]
    if missing:
        warnings.append(
            f"CSVに含まれていないライバーが{len(missing)}人います。"
            f"この人たちの割り当ては変更しません: "
            + "、".join(missing[:10]) + ("… ほか" if len(missing) > 10 else "")
        )

    # --- 差分 ---
    # CSV に載っていないライバーは触らない。そのため現状との比較は
    # 「CSV に出てきたライバー」に限定する。全体を desired で置き換えると、
    # 載っていない人の割り当てが消える。
    touched = {by_account[a][0] for a in seen_accounts}
    current = {(g, s) for g, s in _assignments(conn) if s in touched}
    limited_current = {(g, s) for g, s in current if g in {gid for _, gid in group_columns}}

    added = sorted(desired - limited_current)
    removed = sorted(limited_current - desired)
    name_of = {sid: (acc, nm) for sid, acc, nm in streamers}
    fmt = lambda pair: {
        "group": by_id[pair[0]],
        "groupId": pair[0],
        "username": name_of[pair[1]][0],
        "displayName": name_of[pair[1]][1],
    }
    return {
        "added": [fmt(p) for p in added],
        "removed": [fmt(p) for p in removed],
        "warnings": warnings,
        "streamersInCsv": len(seen_accounts),
        "groupsInCsv": len(group_columns),
        "_desired": sorted(desired),
        "_groupIds": sorted({gid for _, gid in group_columns}),
        "_touched": sorted(touched),
    }


def apply(conn, text: str) -> dict:
    """検証して適用する。**全体を1トランザクションで行う。**

    途中で落ちたら何も変わらない。部分的に適用された状態は、CSVを見ても
    画面を見ても実態が分からなくなるので、いちばん避けたい。
    """
    result = analyze(conn, text)
    desired = {tuple(p) for p in result["_desired"]}
    group_ids = result["_groupIds"]
    touched = result["_touched"]
    if not group_ids or not touched:
        return {k: v for k, v in result.items() if not k.startswith("_")}

    gph = ",".join("?" * len(group_ids))
    sph = ",".join("?" * len(touched))
    try:
        conn.execute("BEGIN IMMEDIATE")
        # CSV に出てきたライバー × CSV にあった列 の範囲だけを入れ替える。
        conn.execute(
            f"DELETE FROM notification_group_streamers "
            f"WHERE group_id IN ({gph}) AND streamer_id IN ({sph})",
            [*group_ids, *touched],
        )
        conn.executemany(
            "INSERT INTO notification_group_streamers (group_id, streamer_id, created_at) "
            "VALUES (?, ?, ?)",
            [(g, s, db.utc_now_iso()) for g, s in sorted(desired)],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {k: v for k, v in result.items() if not k.startswith("_")}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("export", "preview", "apply"))
    p.add_argument("--db-path", required=True)
    args = p.parse_args()

    conn = db.connect(args.db_path)
    try:
        if args.command == "export":
            sys.stdout.write(export_csv(conn))
            return 0
        text = sys.stdin.read()
        try:
            result = analyze(conn, text) if args.command == "preview" else apply(conn, text)
        except CsvError as exc:
            print(json.dumps({"errors": exc.errors}, ensure_ascii=False))
            return 1
        print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")},
                         ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
