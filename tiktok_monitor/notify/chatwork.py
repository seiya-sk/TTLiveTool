"""Chatwork API v2 への送信。

トークンは常に環境変数 CHATWORK_API_TOKEN から読む(引数での明示指定も
可能だが、テスト用の逃げ道であって通常経路ではない)。
"""
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.chatwork.com/v2"
TOKEN_ENV = "CHATWORK_API_TOKEN"

# Chatwork のレート制限は緩い(トークンあたり5分間で数百リクエスト)が、
# 429 が返ったときは素直に待つ。通知は遅れても良いので、諦めるより待つ。
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


class ChatworkError(RuntimeError):
    pass


def get_token(explicit: str | None = None) -> str:
    token = explicit or os.environ.get(TOKEN_ENV, "")
    if not token:
        raise ChatworkError(f"{TOKEN_ENV} が未設定です")
    return token


def format_to(to_account_ids) -> str:
    """[To:123][To:456] 形式。空なら空文字。"""
    if not to_account_ids:
        return ""
    return "".join(f"[To:{str(a).strip()}]" for a in to_account_ids if str(a).strip())


def format_info(title: str, body: str) -> str:
    """Chatwork の [info][title]...[/title]...[/info] ブロック。"""
    return f"[info][title]{title}[/title]{body}[/info]"


def send_message(
    room_id: str,
    body: str,
    to_account_ids=None,
    *,
    token: str | None = None,
    timeout: float = 10.0,
) -> str:
    """送信して message_id を返す。失敗時は ChatworkError。"""
    if not str(room_id).strip():
        raise ChatworkError("room_id が空です")
    resolved = get_token(token)
    message = format_to(to_account_ids) + body

    url = f"{API_BASE}/rooms/{str(room_id).strip()}/messages"
    headers = {"X-ChatWorkToken": resolved}

    last_error = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            # パラメータ名は "body"("message" ではない)。実 API で確認済み --
            # 誤ると HTTP 400 {"errors":["Parameter 'body' is required"]} になる。
            resp = httpx.post(url, headers=headers, data={"body": message}, timeout=timeout)
        except httpx.HTTPError as exc:
            last_error = f"通信エラー: {exc}"
        else:
            if resp.status_code == 200:
                try:
                    return str(resp.json().get("message_id", ""))
                except ValueError:
                    return ""
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code not in _RETRY_STATUSES:
                break  # 401(トークン不正)/404(ルーム不正) は再試行しても無駄
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 30))
        if attempt < _MAX_ATTEMPTS:
            time.sleep(2 ** attempt)

    raise ChatworkError(f"Chatwork 送信に失敗しました (room={room_id}): {last_error}")
