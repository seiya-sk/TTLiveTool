"""通知グループ割り当ての CSV 往復。

事務所規模(100人超)の初期設定・大規模な入れ替えを表計算ソフトで行うための
機能。検証規則が多く、1つ緩むと「設定したつもりが反映されていない」という
気づきにくい形で壊れるので、規則ごとに固定する。
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tiktok_monitor import db
from tiktok_monitor.notify import group_csv


def setup_db(groups=("テスト事務所", "第二事務所"), streamers=("alice", "bob", "carol")):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    gids = []
    for name in groups:
        gids.append(conn.execute(
            "INSERT INTO notification_groups (name, room_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (name, "123", db.utc_now_iso(), db.utc_now_iso())).lastrowid)
    sids = [db.get_or_create_streamer(conn, acc) for acc in streamers]
    conn.commit()
    return conn, gids, sids


def assign(conn, gid, sid):
    conn.execute("INSERT INTO notification_group_streamers (group_id, streamer_id, created_at) "
                 "VALUES (?, ?, ?)", (gid, sid, db.utc_now_iso()))
    conn.commit()


def current(conn):
    return {(g, s) for g, s in conn.execute(
        "SELECT group_id, streamer_id FROM notification_group_streamers")}


# --- エクスポート ---------------------------------------------------------
def test_export_includes_unassigned_streamers():
    """未割り当ても必ず含める。全部0の行が設定漏れの一覧になる。"""
    conn, gids, sids = setup_db()
    assign(conn, gids[0], sids[0])
    text = group_csv.export_csv(conn)
    rows = list(__import__("csv").reader(io.StringIO(text.lstrip(group_csv.BOM))))
    accounts = [r[0] for r in rows[1:]]
    assert accounts == ["alice", "bob", "carol"], "未割り当てが欠けている"
    assert rows[1][2:] == ["1", "0"]
    assert rows[2][2:] == ["0", "0"], "未割り当ての行が 0 になっていない"


def test_export_starts_with_bom_for_excel():
    """Excel は BOM が無いと日本語を Shift_JIS と誤認して文字化けする。"""
    conn, _, _ = setup_db()
    text = group_csv.export_csv(conn)
    assert text.startswith(group_csv.BOM)
    assert text.encode("utf-8").startswith(b"\xef\xbb\xbf")


def test_export_headings_carry_the_group_id():
    """グループ名を変えても手元のCSVが使えるように、IDを併記する。"""
    conn, gids, _ = setup_db()
    text = group_csv.export_csv(conn)
    header = next(__import__("csv").reader(io.StringIO(text.lstrip(group_csv.BOM))))
    assert header[0] == "username" and header[1] == "表示名"
    assert header[2] == f"テスト事務所 [#{gids[0]}]"


def test_export_quotes_group_names_containing_commas():
    """グループ名にカンマ・改行・引用符が入りうる。csv に任せる。"""
    conn, gids, sids = setup_db(groups=('A,社', 'B"社', "C\n社"))
    text = group_csv.export_csv(conn)
    header = next(__import__("csv").reader(io.StringIO(text.lstrip(group_csv.BOM))))
    assert header[2] == f'A,社 [#{gids[0]}]'
    assert header[3] == f'B"社 [#{gids[1]}]'
    assert header[4] == f'C\n社 [#{gids[2]}]'


# --- 列の同定 -------------------------------------------------------------
def test_group_is_identified_by_id_even_after_rename():
    """名前が変わっても ID で同定できること(この機能の主目的)。"""
    conn, gids, sids = setup_db()
    csv_text = f"username,表示名,旧い名前 [#{gids[0]}]\nalice,,1\n"
    result = group_csv.analyze(conn, csv_text)
    assert [a["group"] for a in result["added"]] == ["テスト事務所"]
    assert any("名前が現在の" in w for w in result["warnings"]), "名前の食い違いを警告していない"


def test_group_can_still_be_identified_by_name_alone():
    """手書きのCSVも受け付ける(IDを知らなくても使える)。"""
    conn, gids, sids = setup_db()
    result = group_csv.analyze(conn, "username,表示名,テスト事務所\nalice,,1\n")
    assert len(result["added"]) == 1


# --- エラー(何も変更しない)-----------------------------------------------
@pytest.mark.parametrize("csv_text,fragment", [
    ("username,表示名,テスト事務所\nnobody,,1\n", "登録されていません"),
    ("username,表示名,存在しない事務所\nalice,,1\n", "そのグループは存在しません"),
    ("username,表示名,テスト事務所 [#999]\nalice,,1\n", "グループが存在しません"),
    ("username,表示名,テスト事務所\nalice,,2\n", "1 か 0"),
    ("username,表示名,テスト事務所\nalice,,はい\n", "1 か 0"),
    ("名前,表示名,テスト事務所\nalice,,1\n", "1列目が"),
    ("", "CSVが空です"),
])
def test_invalid_csv_is_rejected(csv_text, fragment):
    conn, gids, sids = setup_db()
    before = current(conn)
    with pytest.raises(group_csv.CsvError) as e:
        group_csv.apply(conn, csv_text)
    assert any(fragment in msg for msg in e.value.errors), e.value.errors
    assert current(conn) == before, "エラーなのにDBが変わっている"


def test_all_errors_are_reported_at_once():
    """1件ずつ直して再アップロードは100人規模だと現実的でない。"""
    conn, gids, sids = setup_db()
    with pytest.raises(group_csv.CsvError) as e:
        group_csv.analyze(conn, "username,表示名,テスト事務所\nnobody,,1\nalice,,5\n")
    assert len(e.value.errors) >= 2, e.value.errors


def test_duplicate_username_is_rejected():
    conn, gids, sids = setup_db()
    with pytest.raises(group_csv.CsvError) as e:
        group_csv.analyze(conn, "username,表示名,テスト事務所\nalice,,1\nalice,,0\n")
    assert any("重複" in m for m in e.value.errors)


# --- 警告(続行可能)-------------------------------------------------------
def test_streamers_missing_from_csv_are_warned_and_left_alone():
    """CSVに無いライバーは触らない。消えると設定が静かに失われる。"""
    conn, gids, sids = setup_db()
    assign(conn, gids[0], sids[2])          # carol は CSV に載せない
    result = group_csv.apply(conn, "username,表示名,テスト事務所\nalice,,1\n")
    assert any("含まれていない" in w for w in result["warnings"])
    assert (gids[0], sids[2]) in current(conn), "CSVに無いライバーの割り当てが消えた"


def test_columns_missing_from_csv_are_left_alone():
    """列に出てこないグループの割り当ても触らない。"""
    conn, gids, sids = setup_db()
    assign(conn, gids[1], sids[0])          # 第二事務所は CSV に載せない
    group_csv.apply(conn, f"username,表示名,テスト事務所 [#{gids[0]}]\nalice,,1\n")
    assert (gids[1], sids[0]) in current(conn), "CSVに無い列の割り当てが消えた"


# --- 差分と適用 -----------------------------------------------------------
def test_preview_shows_which_streamer_moves_where():
    """件数だけでなく、誰がどのグループに追加/削除されるかを見せる。"""
    conn, gids, sids = setup_db()
    assign(conn, gids[0], sids[1])          # bob は既に所属
    result = group_csv.analyze(
        conn, f"username,表示名,テスト事務所 [#{gids[0]}]\nalice,,1\nbob,,0\n")
    assert [(a["username"], a["group"]) for a in result["added"]] == [("alice", "テスト事務所")]
    assert [(r["username"], r["group"]) for r in result["removed"]] == [("bob", "テスト事務所")]


def test_preview_does_not_change_anything():
    conn, gids, sids = setup_db()
    before = current(conn)
    group_csv.analyze(conn, "username,表示名,テスト事務所\nalice,,1\n")
    assert current(conn) == before


def test_apply_is_atomic_on_failure():
    """途中で落ちたら何も変わらないこと。部分適用は実態が分からなくなる。

    sqlite3.Connection のメソッドは差し替えられないので、挿入だけ失敗する
    薄い代理を挟む。DELETE は本物に通るため、「削除は済んだが挿入で落ちた」
    という最悪の中間状態を実際に作れる。
    """
    conn, gids, sids = setup_db()
    assign(conn, gids[0], sids[1])
    before = current(conn)

    class FailingInsert:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def executemany(self, *a, **kw):
            raise RuntimeError("挿入中に失敗")

    with pytest.raises(RuntimeError):
        group_csv.apply(FailingInsert(conn),
                        f"username,表示名,テスト事務所 [#{gids[0]}]\nalice,,1\nbob,,0\n")

    assert current(conn) == before, "失敗したのに一部だけ適用されている"


def test_blank_cell_is_treated_as_zero():
    """表計算ソフトは 0 を空欄にしがち。ここでエラーにすると実用に耐えない。"""
    conn, gids, sids = setup_db()
    assign(conn, gids[0], sids[0])
    result = group_csv.apply(conn, f"username,表示名,テスト事務所 [#{gids[0]}]\nalice,,\n")
    assert [r["username"] for r in result["removed"]] == ["alice"]


def test_round_trip_is_stable():
    """出して入れて何も変わらないこと。往復編集の前提。"""
    conn, gids, sids = setup_db()
    assign(conn, gids[0], sids[0]); assign(conn, gids[1], sids[2])
    before = current(conn)
    result = group_csv.apply(conn, group_csv.export_csv(conn))
    assert result["added"] == [] and result["removed"] == []
    assert current(conn) == before


def test_handles_a_thousand_rows():
    """1000行程度でも処理できること。"""
    conn, gids, _ = setup_db(streamers=())
    accounts = [f"user{i:04d}" for i in range(1000)]
    for a in accounts:
        db.get_or_create_streamer(conn, a)
    conn.commit()
    lines = [f"username,表示名,テスト事務所 [#{gids[0]}]"]
    lines += [f"{a},,{1 if i % 2 == 0 else 0}" for i, a in enumerate(accounts)]
    result = group_csv.apply(conn, "\n".join(lines) + "\n")
    assert len(result["added"]) == 500
    assert len(current(conn)) == 500
