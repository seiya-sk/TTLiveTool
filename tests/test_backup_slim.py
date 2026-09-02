"""バックアップの軽量化(生ペイロードの除外)。

バックアップの目的は「集計済みのデータを失わないこと」。生ペイロードは
稼働中DBでも保持1日で消える一時データなので、世代を重ねて抱える意味がない。
ただし **落としてよいのは生ペイロードだけ** で、live_events や
live_sessions が1件でも欠けたらバックアップとして失格になる。
そこを固定する。
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

import backup_db
from tiktok_monitor import db


def make_db(path, n_events=200, payload_bytes=20_000):
    conn = db.connect(str(path))
    db.init_schema(conn)
    sid = db.create_live_session(conn, db.get_or_create_streamer(conn, "someone"))
    for i in range(n_events):
        eid = conn.execute(
            "INSERT INTO live_events (live_session_id, event_type, occurred_at) VALUES (?,?,?)",
            (sid, "comment", "2026-09-02T00:00:00+00:00"),
        ).lastrowid
        conn.execute(
            "INSERT INTO live_event_raw_payloads (live_event_id, raw_payload) VALUES (?,?)",
            (eid, b"x" * payload_bytes),
        )
    conn.commit()
    conn.close()
    return sid


def test_backup_drops_raw_payloads_but_keeps_everything_else(tmp_path):
    src = tmp_path / "live.db"
    make_db(src)
    dest = tmp_path / "backup.db"

    backup_db.take_backup(str(src), str(dest))

    out = sqlite3.connect(str(dest))
    assert out.execute("SELECT COUNT(*) FROM live_events").fetchone()[0] == 200, \
        "イベントが失われている"
    assert out.execute("SELECT COUNT(*) FROM live_sessions").fetchone()[0] == 1, \
        "セッションが失われている"
    assert out.execute("SELECT COUNT(*) FROM streamers").fetchone()[0] == 1
    assert out.execute("SELECT COUNT(*) FROM live_event_raw_payloads").fetchone()[0] == 0, \
        "生ペイロードが残っている(軽量化できていない)"
    out.close()


def test_the_backup_file_actually_shrinks(tmp_path):
    """DELETE だけではファイルは縮まない(auto_vacuum=0)。VACUUM まで
    到達していることを、ファイルサイズで確認する。"""
    src = tmp_path / "live.db"
    make_db(src)
    full = tmp_path / "full.db"
    slim = tmp_path / "slim.db"

    backup_db.take_backup(str(src), str(full), keep_raw_payloads=True)
    backup_db.take_backup(str(src), str(slim))

    assert os.path.getsize(slim) < os.path.getsize(full) / 2, (
        f"縮んでいない: full={os.path.getsize(full)} slim={os.path.getsize(slim)}"
    )


def test_the_live_db_is_never_modified(tmp_path):
    """**稼働中DBには触らない。** 触るのは自分が作ったコピーだけ。
    ここが壊れると録画中のデータを消すことになる。"""
    src = tmp_path / "live.db"
    make_db(src)
    before = sqlite3.connect(str(src)).execute(
        "SELECT COUNT(*) FROM live_event_raw_payloads").fetchone()[0]

    backup_db.take_backup(str(src), str(tmp_path / "backup.db"))

    after = sqlite3.connect(str(src)).execute(
        "SELECT COUNT(*) FROM live_event_raw_payloads").fetchone()[0]
    assert after == before == 200, "稼働中DBの生ペイロードが消えた"


def test_no_temporary_files_are_left_behind(tmp_path):
    src = tmp_path / "live.db"
    make_db(src)
    dest = tmp_path / "backup.db"
    backup_db.take_backup(str(src), str(dest))
    leftovers = [p.name for p in tmp_path.iterdir()
                 if ".partial" in p.name or ".slim" in p.name]
    assert leftovers == [], f"一時ファイルが残っている: {leftovers}"
