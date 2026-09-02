"""Healthchecks.io へのハートビート送信。

死活監視は「呼び出し側を壊さない」ことが最優先なので、この関数は例外を
投げず bool を返す。監視の失敗が本体の失敗を引き起こしては本末転倒。

無条件に ping を打ってはいけない点に注意 -- それは timer が生きている
ことしか証明しない。呼び出し側で健全性を判定し、正常なら ping()、
異常なら fail() を打ち分けること。
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

PING_BASE = "https://hc-ping.com"
UUID_ENV = "HEALTHCHECKS_UUID"


def _post(suffix: str, uuid: str | None, payload: str | None, timeout: float) -> bool:
    resolved = uuid or os.environ.get(UUID_ENV, "")
    if not resolved:
        logger.debug("%s が未設定のため Healthchecks への送信をスキップ", UUID_ENV)
        return False
    url = f"{PING_BASE}/{resolved}{suffix}"
    try:
        resp = httpx.post(url, content=(payload or "")[:10000].encode("utf-8"), timeout=timeout)
        if resp.status_code == 200:
            return True
        logger.warning("Healthchecks への送信が HTTP %s (%s)", resp.status_code, suffix or "/")
    except httpx.HTTPError as exc:
        logger.warning("Healthchecks への送信に失敗: %s", exc)
    return False


def ping(uuid: str | None = None, payload: str | None = None, timeout: float = 10.0) -> bool:
    """正常。これが一定時間途絶えると Healthchecks 側が警報を出す。"""
    return _post("", uuid, payload, timeout)


def fail(uuid: str | None = None, payload: str | None = None, timeout: float = 10.0) -> bool:
    """異常。待たずに即座に警報させる。"""
    return _post("/fail", uuid, payload, timeout)


def start(uuid: str | None = None, timeout: float = 10.0) -> bool:
    """ジョブ開始。/start と終了コードの組で実行時間も追跡できる。"""
    return _post("/start", uuid, None, timeout)


def exit_code(code: int, uuid: str | None = None, payload: str | None = None, timeout: float = 10.0) -> bool:
    return _post(f"/{int(code)}", uuid, payload, timeout)
