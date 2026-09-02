import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.events import filter_battle_opponents, find_display_ids


def test_find_display_ids_extracts_from_nested_payload():
    payload = {
        "armies": [
            {"user": {"display_id": "opponent_streamer", "nickname": "Opponent"}},
            {"user": {"display_id": "my_own_handle", "nickname": "Me"}},
        ],
        "battle_id": 12345,
    }
    assert find_display_ids(payload) == ["opponent_streamer", "my_own_handle"]


def test_find_display_ids_deduplicates_preserving_order():
    payload = {"a": {"display_id": "user_a"}, "b": {"display_id": "user_a"}}
    assert find_display_ids(payload) == ["user_a"]


def test_find_display_ids_returns_empty_for_no_match():
    assert find_display_ids({"nothing": "here"}) == []


def test_filter_battle_opponents_drops_self_and_placeholders():
    candidates = ["opponent_streamer", "my_own_handle", "0", "None", ""]
    result = filter_battle_opponents(candidates, own_username="my_own_handle")
    assert result == ["opponent_streamer"]


def test_filter_battle_opponents_matches_self_as_substring():
    candidates = ["renamaru_roilala_backup"]
    result = filter_battle_opponents(candidates, own_username="renamaru_roilala")
    assert result == []


def test_filter_battle_opponents_is_case_insensitive():
    candidates = ["MyOwnHandle"]
    result = filter_battle_opponents(candidates, own_username="myownhandle")
    assert result == []
