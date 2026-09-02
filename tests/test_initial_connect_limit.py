"""初回接続のリトライ上限(kyutyom 事案の再発防止)。

2026-09-01、@kyutyom は WSハンドシェイクで InvalidStatusCode(400) を
**13回連続** で返し、13回すべて同じプロキシIP(#10)から試行された。
is_live は別エンドポイントなので True を返し続け、run_with_reconnect の
「まだ配信中なのだから再試行」分岐が回り続けた。署名13個、イベント0件。

署名URLは30秒で失効し再利用できないので、再試行1回 = 署名1つ。
ここを塞ぐのがこのテストの目的。

**ストール再接続の上限(MAX_STALL_RECONNECT_RETRIES)とは別経路**であること、
とくに「一度繋がったセッション」には効かないことを固定する。一度録れている
ライブを1回の失敗で手放すと、この上限が逆にデータを失わせる。
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import db
from tiktok_monitor.client import SessionRunner, run_with_reconnect
from tiktok_monitor.config import Settings


class _StopLoop(Exception):
    """再接続ループが上限で降りずに回り続けたことを検出するための番人。"""


def make_runner(username="kyutyom"):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(username=username, db_path=":memory:", idle_timeout_sec=60)
    return SessionRunner(conn, settings), conn


def invalid_status_code(status=400):
    """本番で実際に上がる例外そのものを使う(自作のダミーではなく)。
    kyutyom の13連続失敗はすべてこれだった。"""
    from websockets.exceptions import InvalidStatusCode
    return InvalidStatusCode(status, {})


def run_scenario(*, side_effect, max_initial, connected=False, room_id="ROOM_A"):
    """run_with_reconnect を1つのフェイククライアントで回し、
    通知された status の一覧を返す。"""
    statuses = []

    async def scenario():
        runner, _conn = make_runner()
        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=side_effect)
        fake_client.connected = False
        fake_client.room_id = room_id

        def on_status(kind, info):
            statuses.append((kind, info))
            # 「繋がった」状態を作るための細工。ever_connected を立てる。
            if kind == "force_connected":
                pass

        with patch.object(SessionRunner, "build_client", return_value=fake_client):
            with patch("asyncio.sleep", AsyncMock(side_effect=_StopLoop)):
                # 一度も繋がっていない状況では is_live=True を返させる
                # (これが kyutyom 事案の条件。ここが False だと自然終了扱いになる)
                try:
                    await run_with_reconnect(
                        runner, runner.settings,
                        on_status=on_status,
                        check_is_live_fn=AsyncMock(return_value=True),
                        max_initial_connect_failures=max_initial,
                    )
                except _StopLoop:
                    statuses.append(("__looped_again__", {}))
        runner.manual_end()

    asyncio.run(scenario())
    return statuses


def test_initial_connection_gives_up_at_the_cap_instead_of_retrying():
    statuses = run_scenario(side_effect=invalid_status_code(), max_initial=1)
    kinds = [k for k, _ in statuses]
    assert "gave_up_initial_connect" in kinds, f"上限で降りていない: {kinds}"
    assert "__looped_again__" not in kinds, \
        "上限に達したのに再試行のスリープに入った(署名を使い続ける)"


def test_the_give_up_notice_carries_the_room_id_and_error_type():
    """呼び出し側(プール)が (username, room_id) 単位で数えるために要る。
    どちらが欠けても門番が機能しない。"""
    statuses = run_scenario(side_effect=invalid_status_code(), max_initial=1)
    info = dict(statuses)["gave_up_initial_connect"]
    assert info["room_id"] == "ROOM_A"
    assert info["error_type"] == "InvalidStatusCode"


def test_without_the_cap_the_old_unlimited_retry_behaviour_is_unchanged():
    """main.py / watch.py は上限を渡さない(人が見ていて Ctrl+C できる)。
    既定の挙動を変えていないことを固定する。"""
    statuses = run_scenario(side_effect=invalid_status_code(), max_initial=None)
    kinds = [k for k, _ in statuses]
    assert "gave_up_initial_connect" not in kinds
    assert "__looped_again__" in kinds, "上限なしなのに再試行しなかった"


def test_the_cap_does_not_apply_once_the_session_has_connected():
    """**一度繋がったライブには効かない。** 録画実績のあるセッションを
    1回の失敗で手放すと、この上限がデータを失わせる側に回る。
    そちらは max_reconnects_per_live が担当する別の経路。"""
    attempts = {"n": 0}
    captured = {}
    statuses = []

    async def fake_start(*_a, **_kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # 1回目は接続に成功した、と run_with_reconnect に伝える。
            captured["cb"]("connected", {})
            return AsyncMock()()
        raise invalid_status_code()

    async def scenario():
        runner, _conn = make_runner()
        fake_client = MagicMock()
        fake_client.start = AsyncMock(side_effect=fake_start)
        fake_client.connected = False
        fake_client.room_id = "ROOM_A"

        def build(on_status=None, **_kw):
            captured["cb"] = on_status
            return fake_client

        with patch.object(SessionRunner, "build_client", side_effect=build):
            with patch("asyncio.sleep", AsyncMock(side_effect=_StopLoop)):
                try:
                    await run_with_reconnect(
                        runner, runner.settings,
                        on_status=lambda k, i: statuses.append(k),
                        check_is_live_fn=AsyncMock(return_value=True),
                        max_initial_connect_failures=1,
                    )
                except _StopLoop:
                    statuses.append("__looped_again__")
        runner.manual_end()

    asyncio.run(scenario())

    assert "connected" in statuses, "テストの前提が崩れている(接続できていない)"
    assert "gave_up_initial_connect" not in statuses, \
        "一度繋がったセッションを初回接続の上限で手放した(録画データを失う)"
    assert "__looped_again__" in statuses, "接続実績のあるライブを再接続しなかった"
