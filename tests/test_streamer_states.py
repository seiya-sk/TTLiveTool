"""ライバーの3状態(有効 / 無効 / アーカイブ)。

状態は2つの軸で表す:
    有効       archived=0, enabled=1
    無効       archived=0, enabled=0
    アーカイブ  archived=1(enabled は意味を持たない)

archived と統合して status 1列にすることも検討したが、既存の
`archived = 0` フィルタが「通常の一覧(有効+無効)」の意味をそのまま
保てるため、こちらを採った。その前提が崩れていないことを固定する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tiktok_monitor import db


def make(conn=None):
    conn = conn or db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_new_streamers_are_enabled_by_default():
    conn = make()
    sid = db.get_or_create_streamer(conn, "alice")
    row = next(s for s in db.list_streamers(conn) if s["id"] == sid)
    assert row["enabled"] is True and row["archived"] is False


def test_migration_treats_existing_rows_as_enabled(tmp_path):
    """enabled 列が無かった頃の行は、すべて有効として扱う。
    無効という状態はこの列を足すまで存在しなかったので他の解釈がない。"""
    path = tmp_path / "old.db"
    conn = db.connect(str(path))
    db.init_schema(conn)
    conn.execute("ALTER TABLE streamers DROP COLUMN enabled")
    conn.execute("INSERT INTO streamers (name, tiktok_account_id, created_at) VALUES (?,?,?)",
                 ("旧データ", "legacy", db.utc_now_iso()))
    conn.commit()
    conn.close()

    conn = db.connect(str(path))
    db.init_schema(conn)          # ここで移行が走る
    row = next(s for s in db.list_streamers(conn) if s["tiktok_account_id"] == "legacy")
    assert row["enabled"] is True


def test_disabling_keeps_the_streamer_in_the_normal_roster():
    """無効は「通常の一覧に表示する」状態。archived を立ててはいけない。"""
    conn = make()
    sid = db.get_or_create_streamer(conn, "alice")
    db.set_streamer_enabled(conn, sid, False)

    row = next(s for s in db.list_streamers(conn) if s["id"] == sid)
    assert row["enabled"] is False
    assert row["archived"] is False, "無効にしたらアーカイブされている"
    # include_archived=False は「アーカイブでないもの」= 有効+無効
    assert any(s["id"] == sid for s in db.list_streamers(conn, include_archived=False))


def test_archiving_removes_from_the_normal_roster():
    conn = make()
    sid = db.get_or_create_streamer(conn, "alice")
    db.archive_streamer(conn, sid)
    assert not any(s["id"] == sid for s in db.list_streamers(conn, include_archived=False))
    assert any(s["id"] == sid for s in db.list_streamers(conn))


def test_enabled_and_archived_are_independent_axes():
    """無効のままアーカイブでき、状態が混ざらないこと。"""
    conn = make()
    sid = db.get_or_create_streamer(conn, "alice")
    db.set_streamer_enabled(conn, sid, False)
    db.archive_streamer(conn, sid)
    row = next(s for s in db.list_streamers(conn) if s["id"] == sid)
    assert row["archived"] is True and row["enabled"] is False


def test_past_sessions_survive_every_state_change():
    """**過去データは状態にかかわらず保持する。**
    アーカイブしたライバーの過去ライブが見られなくなるのは避ける。"""
    conn = make()
    sid = db.get_or_create_streamer(conn, "alice")
    session = db.create_live_session(conn, sid, room_id="ROOM_A")
    db.insert_event(conn, session, "comment", None, None, {"t": "x"}, {"raw": "y"})
    db.end_session(conn, session, "live_end")

    for change in (lambda: db.set_streamer_enabled(conn, sid, False),
                   lambda: db.archive_streamer(conn, sid),
                   lambda: db.unarchive_streamer(conn, sid)):
        change()
        assert conn.execute(
            "SELECT COUNT(*) FROM live_sessions WHERE streamer_id = ?", (sid,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM live_events WHERE live_session_id = ?", (session,)).fetchone()[0] == 1


def test_archived_streamers_are_excluded_from_notification_candidates():
    """通知グループの割り当て候補からアーカイブ済みは外れるが、
    無効は候補に残る(在籍しているため)。"""
    from tiktok_monitor.notify import group_csv
    conn = make()
    conn.execute("INSERT INTO notification_groups (name, room_id, created_at, updated_at) "
                 "VALUES (?,?,?,?)", ("G", "1", db.utc_now_iso(), db.utc_now_iso()))
    disabled = db.get_or_create_streamer(conn, "disabled_one")
    archived = db.get_or_create_streamer(conn, "archived_one")
    db.set_streamer_enabled(conn, disabled, False)
    db.archive_streamer(conn, archived)
    conn.commit()

    accounts = [r[1] for r in group_csv._streamers(conn)]
    assert "disabled_one" in accounts, "無効が候補から消えている"
    assert "archived_one" not in accounts, "アーカイブ済みが候補に残っている"
