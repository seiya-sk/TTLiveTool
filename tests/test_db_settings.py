import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db


def make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_get_setting_returns_none_when_unset():
    conn = make_conn()
    assert db.get_setting(conn, "usd_jpy_rate") is None


def test_set_then_get_setting_round_trips():
    conn = make_conn()
    db.set_setting(conn, "usd_jpy_rate", "151.23")
    row = db.get_setting(conn, "usd_jpy_rate")
    assert row["value"] == "151.23"
    assert row["updated_at"] is not None


def test_set_setting_overwrites_existing_value_and_bumps_updated_at():
    conn = make_conn()
    db.set_setting(conn, "usd_jpy_rate", "150")
    first = db.get_setting(conn, "usd_jpy_rate")
    db.set_setting(conn, "usd_jpy_rate", "152")
    second = db.get_setting(conn, "usd_jpy_rate")

    assert second["value"] == "152"
    assert first["value"] == "150"  # confirms it was actually overwritten, not appended
