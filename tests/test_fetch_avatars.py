import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.fetch_avatars import _extract_avatar_url, default_avatar_path


def test_extract_avatar_url_picks_first_url_from_avatar_medium():
    room_info = {
        "owner": {
            "avatar_medium": {
                "url_list": [
                    "https://p19-common-sign.tiktokcdn.com/avatar1.webp?x-expires=1",
                    "https://p16-common-sign.tiktokcdn.com/avatar1-mirror.webp?x-expires=1",
                ]
            }
        }
    }
    assert _extract_avatar_url(room_info) == "https://p19-common-sign.tiktokcdn.com/avatar1.webp?x-expires=1"


def test_extract_avatar_url_returns_none_when_owner_missing():
    assert _extract_avatar_url({}) is None


def test_extract_avatar_url_returns_none_when_url_list_empty():
    room_info = {"owner": {"avatar_medium": {"url_list": []}}}
    assert _extract_avatar_url(room_info) is None


def test_default_avatar_path_joins_dir_and_account_id():
    # os.path.join, not manual string concat -- matches screenshot.py's
    # default_screenshot_path, backslash-joined on Windows.
    assert default_avatar_path("data/avatars", "chanhika825") == os.path.join("data/avatars", "chanhika825.webp")
