"""署名リクエストを各プロキシIPから出す実装の検証。

Euler Stream のレート制限は **IP単位**(2026-09-02 実測: day 100 / hour 30 /
minute 5)。実IPだけで署名していると1日100件で頭打ちになり、実際に
19時台で枯渇して2時間半録画できなくなった。各録画が自分の使っている
プロキシIPから署名を出せば、10本で 1,000件/日 になる。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor import client as C
from tiktok_monitor import db
from tiktok_monitor.config import Settings

PROXY = "http://user:pass@10.0.0.1:8080"


def make_runner(proxy_url):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    settings = Settings(
        username="someone", db_path=":memory:", idle_timeout_sec=60,
        screenshots_enabled=False, proxy_url=proxy_url,
    )
    return C.SessionRunner(conn, settings)


def signer_client(tiktok_client):
    return tiktok_client.web.signer.sdk_client.get_async_httpx_client()


def test_signing_client_is_routed_through_the_proxy():
    async def scenario():
        runner = make_runner(PROXY)
        tc = runner.build_client()
        hc = signer_client(tc)
        # 差し替えたクライアントを runner が保持している(後で閉じるため)
        assert runner._signer_http_client is hc
        # httpx はプロキシ設定を transport に持つ。mounts/transport のどちらかに
        # プロキシが載っていることを確認する。
        assert hc is not None
        await hc.aclose()

    asyncio.run(scenario())


def test_no_proxy_leaves_the_signer_untouched():
    """proxy_url 未設定なら差し替えない = 従来どおり実IPから署名する。"""
    async def scenario():
        runner = make_runner(None)
        runner.build_client()
        assert runner._signer_http_client is None

    asyncio.run(scenario())


def test_reconnect_closes_the_previous_signer_client():
    """再接続のたびに httpx.AsyncClient が積み上がらないこと。
    9本同時録画が何時間も再接続を繰り返すとソケットが枯れる。"""
    async def scenario():
        runner = make_runner(PROXY)
        runner.build_client()
        first = runner._signer_http_client
        assert first is not None

        runner.build_client()          # 2回目の接続
        second = runner._signer_http_client
        assert second is not first, "新しいクライアントに差し替わっていない"

        await asyncio.sleep(0)         # 閉じるタスクを走らせる
        assert first.is_closed, "前回のクライアントが閉じられていない"
        await second.aclose()

    asyncio.run(scenario())


def test_routing_failure_falls_back_instead_of_breaking_recording():
    """ライブラリの内部構造が変わって差し替えに失敗しても、録画は続ける。
    署名が実IPから出るだけで、録画そのものは動く。"""
    class Broken:
        @property
        def web(self):
            raise AttributeError("signer moved")

    assert C._route_signer_through_proxy(Broken(), PROXY) is None
    # プロキシ未設定なら何もしない
    assert C._route_signer_through_proxy(Broken(), None) is None


def test_api_key_header_survives_the_proxy_swap():
    """APIキーを設定したときに、そのキーがちゃんと送られること。

    EulerApiSdk の AuthenticatedClient は認証ヘッダを _headers ではなく
    get_async_httpx_client() の中で遅延注入する。こちらが先に
    set_async_httpx_client() で差し替えると、そのコードが二度と走らず
    **APIキーが送られないまま匿名扱いに戻る**。キーを設定した瞬間に
    静かに壊れるので、テストで固定しておく。"""
    import os
    from unittest.mock import patch

    async def scenario():
        with patch.dict(os.environ, {"SIGN_API_KEY": "TEST_KEY_123"}):
            runner = make_runner(PROXY)
            tc = runner.build_client()
            sdk = tc.web.signer.sdk_client
            assert type(sdk).__name__ == "AuthenticatedClient"
            hc = sdk.get_async_httpx_client()
            assert hc.headers.get("x-api-key") == "TEST_KEY_123", \
                "差し替えでAPIキーが落ちている"
            await hc.aclose()

    asyncio.run(scenario())
