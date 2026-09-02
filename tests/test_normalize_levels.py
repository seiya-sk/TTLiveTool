import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.events import (
    normalize_comment,
    normalize_follow,
    normalize_gift,
    normalize_room_enter,
)

# Real badge_list entries carry the level under scene_type/privilege_log_extra.level
# (see events.py's _badge_level docstring for why -- the installed TikTokLive
# version's own gifter_level/member_level properties read badge_scene/log_extra,
# which don't exist on real objects). Scene 8 = USER_GRADE (gifter level),
# scene 10 = FANS (this streamer's member level).
def badge(scene_type, level):
    return SimpleNamespace(scene_type=scene_type, privilege_log_extra=SimpleNamespace(level=str(level)))


def make_event(badge_list=None, pay_grade=None):
    user = SimpleNamespace(
        unique_id="u1", nickname="Nick", badge_list=badge_list or [], pay_grade=pay_grade
    )
    return SimpleNamespace(user=user)


def test_normalize_gift_includes_gifter_level():
    event = make_event(badge_list=[badge(8, 5)])
    event.gift = SimpleNamespace(id=1, name="Rose", type=1, diamond_count=1)
    event.repeat_count = 1
    event.repeat_end = 1
    event.streaking = False
    event.value = 0.005

    _, _, _, payload = normalize_gift(event)
    assert payload["gifter_level"] == 5


def test_normalize_gift_falls_back_to_pay_grade_level_when_no_grade_badge():
    event = make_event(badge_list=[], pay_grade=SimpleNamespace(level=7))
    event.gift = SimpleNamespace(id=1, name="Rose", type=1, diamond_count=1)
    event.repeat_count = 1
    event.repeat_end = 1
    event.streaking = False
    event.value = 0.005

    _, _, _, payload = normalize_gift(event)
    assert payload["gifter_level"] == 7


def test_normalize_follow_includes_gifter_level():
    event = make_event(badge_list=[badge(8, 3)])
    _, _, _, payload = normalize_follow(event)
    assert payload["gifter_level"] == 3


def test_normalize_room_enter_includes_gifter_level():
    event = make_event(badge_list=[badge(8, 2)])
    _, _, _, payload = normalize_room_enter(event)
    assert payload["gifter_level"] == 2


def test_normalize_comment_includes_gifter_and_member_level():
    event = make_event(badge_list=[badge(8, 4), badge(10, 10)])
    event.comment = "hello"
    _, _, _, payload = normalize_comment(event)
    assert payload["gifter_level"] == 4
    assert payload["member_level"] == 10


def test_normalize_comment_member_level_is_none_when_no_fans_badge():
    # Not being in this streamer's fan club is a legitimate state, not a
    # lookup failure -- must stay None rather than fabricating a level.
    event = make_event(badge_list=[badge(8, 1)])
    event.comment = "hi"
    _, _, _, payload = normalize_comment(event)
    assert payload["gifter_level"] == 1
    assert payload["member_level"] is None


def test_normalize_functions_tolerate_missing_user():
    event = SimpleNamespace(user=None, comment="hi")
    assert normalize_comment(event)[3]["gifter_level"] is None
    assert normalize_follow(event)[3]["gifter_level"] is None
    assert normalize_room_enter(event)[3]["gifter_level"] is None


def test_badge_with_unrecognized_scene_is_ignored():
    event = make_event(badge_list=[badge(99, 999), badge(8, 6)])
    event.gift = SimpleNamespace(id=1, name="Rose", type=1, diamond_count=1)
    event.repeat_count = 1
    event.repeat_end = 1
    event.streaking = False
    event.value = 0.005

    _, _, _, payload = normalize_gift(event)
    assert payload["gifter_level"] == 6
