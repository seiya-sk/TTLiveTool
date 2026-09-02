"""Normalizes TikTokLive event objects into the (event_type, user_id, user_nickname, payload)
shape stored in live_events. Field access is defensive (getattr-based) because exact
attribute names can shift slightly across TikTokLive versions; raw_payload always keeps
the full serialized event so nothing is lost even if a specific field lookup misses.
"""
import base64
import enum
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Ported from a previously working scraper: TikTok's LinkMic/Battle/Armies
# webcast messages carry the opposing streamer's handle as a `display_id`
# field nested somewhere in per-anchor info, but the exact nesting path
# varies across event types (and has shifted across library/protocol
# versions before). Rather than pin to one field path, scan the full
# serialized event text for any `display_id` occurrence.
_DISPLAY_ID_PATTERN = re.compile(r"display_id['\"]?\s*[:=]\s*['\"]?([a-zA-Z0-9_.-]+)['\"]?")

_MAX_SERIALIZE_DEPTH = 15


def _to_jsonable(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_SERIALIZE_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, depth + 1) for k, v in value.items()}
    if hasattr(value, "__dict__"):
        return {k: _to_jsonable(v, depth + 1) for k, v in vars(value).items() if not k.startswith("_")}
    return repr(value)


def safe_serialize(obj: Any) -> dict:
    """Walks the event's actual instance attributes (vars()) rather than
    calling the library's to_dict()/model_dump(): the installed TikTokLive
    (betterproto2-based) raises NameError from its TYPE_CHECKING-only field
    annotations as soon as a real (non-default) event is serialized, so
    those methods are unusable here. Reflection over vars() sidesteps that
    broken annotation-based introspection entirely."""
    try:
        result = _to_jsonable(obj)
        if isinstance(result, dict):
            return result
    except Exception as exc:
        logger.debug("safe_serialize: reflection walk failed: %r", exc)
    return {"repr": repr(obj)}


def extract_occurred_at(event: Any) -> str | None:
    """Most webcast events carry event.common.create_time (epoch millis) set
    by TikTok itself; prefer it over local receive time when present."""
    common = getattr(event, "common", None)
    create_time = getattr(common, "create_time", None) if common else None
    if not create_time:
        return None
    return datetime.fromtimestamp(create_time / 1000, tz=timezone.utc).isoformat()


def _user_info(event: Any) -> tuple[str | None, str | None]:
    user = getattr(event, "user", None)
    if user is None:
        return None, None
    user_id = (
        getattr(user, "unique_id", None)
        or getattr(user, "user_id", None)
        or getattr(user, "id", None)
    )
    nickname = getattr(user, "nickname", None)
    return (str(user_id) if user_id is not None else None), nickname


# TikTokLive's own User.gifter_level/.member_level properties (proto/custom_proto.py)
# read badge.badge_scene and badge.log_extra.level -- but the installed
# TikTokLive version's vendored tiktok_proto.py is stale relative to the
# actual wire schema: real BadgeStruct instances carry the level under
# badge.scene_type / badge.privilege_log_extra.level instead (confirmed by
# dumping session 9's raw_payload -- badge.badge_scene/.log_extra are always
# absent, so those library properties always silently return None). This is
# the same class of stale-codegen bug already documented on safe_serialize's
# to_dict()/model_dump() breakage; reading the real attribute names directly
# sidesteps it the same way. Scene numbers are TikTok's own enum values
# (BadgeStructBadgeSceneType in tiktok_proto.py): 8 = USER_GRADE (overall
# gifter level), 10 = FANS (this streamer's fan/member level).
_BADGE_SCENE_USER_GRADE = 8
_BADGE_SCENE_FANS = 10


def _badge_level(user: Any, target_scene: int) -> int | None:
    for badge in getattr(user, "badge_list", None) or []:
        scene = getattr(badge, "scene_type", None)
        if scene is None:
            continue
        try:
            if int(scene) != target_scene:
                continue
        except (TypeError, ValueError):
            continue
        log_extra = getattr(badge, "privilege_log_extra", None)
        level = getattr(log_extra, "level", None) if log_extra is not None else None
        try:
            return int(level) if level is not None else None
        except (TypeError, ValueError):
            continue
    return None


def _gift_level(event: Any) -> int | None:
    """Overall TikTok gifter level (badge scene USER_GRADE), falling back to
    pay_grade.level when the badge is absent (e.g. brand-new accounts with
    no level badge yet)."""
    user = getattr(event, "user", None)
    if user is None:
        return None
    level = _badge_level(user, _BADGE_SCENE_USER_GRADE)
    if level is not None:
        return level
    pay_grade = getattr(user, "pay_grade", None)
    raw = getattr(pay_grade, "level", None) if pay_grade else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _member_level(event: Any) -> int | None:
    """This streamer's fan/member level (badge scene FANS) -- unset when the
    user hasn't joined this streamer's fan club, not a lookup failure."""
    user = getattr(event, "user", None)
    if user is None:
        return None
    return _badge_level(user, _BADGE_SCENE_FANS)


def normalize_comment(event: Any) -> tuple[str, str | None, str | None, dict]:
    user_id, nickname = _user_info(event)
    payload = {
        "comment": getattr(event, "comment", None),
        "gifter_level": _gift_level(event),
        "member_level": _member_level(event),
    }
    return "comment", user_id, nickname, payload


def normalize_gift(event: Any) -> tuple[str, str | None, str | None, dict]:
    user_id, nickname = _user_info(event)
    gift = getattr(event, "gift", None)
    payload = {
        "gift_id": getattr(gift, "id", None),
        "gift_name": getattr(gift, "name", None),
        "gift_type": getattr(gift, "type", None),
        "diamond_count": getattr(gift, "diamond_count", None),
        "repeat_count": getattr(event, "repeat_count", None),
        "repeat_end": getattr(event, "repeat_end", None),
        "streaking": getattr(event, "streaking", None),
        "value_usd": getattr(event, "value", None),
        "gifter_level": _gift_level(event),
        # Promoted from raw_payload into this small curated payload so the
        # gift-dedup queries (report/data.py, dashboard queries.ts) never
        # need to touch raw_payload -- see db.py's live_events comment for
        # why that split exists and matters for read performance.
        "log_id": getattr(event, "log_id", None),
    }
    return "gift", user_id, nickname, payload


def normalize_viewer_count(event: Any) -> tuple[str, str | None, str | None, dict]:
    payload = {
        "viewer_count": getattr(event, "total", None),
        "total_unique_viewers": getattr(event, "total_user", None),
    }
    return "viewer_count", None, None, payload


def normalize_like(event: Any) -> tuple[str, str | None, str | None, dict]:
    user_id, nickname = _user_info(event)
    payload = {
        "like_count": getattr(event, "count", None),
        "total_likes": getattr(event, "total", None),
    }
    return "like", user_id, nickname, payload


def normalize_follow(event: Any) -> tuple[str, str | None, str | None, dict]:
    user_id, nickname = _user_info(event)
    return "follow", user_id, nickname, {"gifter_level": _gift_level(event)}


def normalize_share(event: Any) -> tuple[str, str | None, str | None, dict]:
    user_id, nickname = _user_info(event)
    return "share", user_id, nickname, {}


def normalize_room_enter(event: Any) -> tuple[str, str | None, str | None, dict]:
    user_id, nickname = _user_info(event)
    return "room_enter", user_id, nickname, {"gifter_level": _gift_level(event)}


# --- Treasure Box -----------------------------------------------------
#
# TikTok's "Treasure Box" and its unrelated "Red Envelope" feature are both
# delivered through the same WebcastEnvelopeMessage/EnvelopeEvent wire
# message -- confirmed empirically against real captured data
# (sample/debug_event/EnvelopeEvent.jsonl): of its 10 events, 4 are Treasure
# Box sends (common.display_text.key == 'pm_mt_treasure_box_sender_comment',
# envelope_info.diamond_count/people_count/unpack_at all populated) and 6
# are Red Envelope (empty key, no coin data). display_text.key is the only
# reliable discriminator -- NOT the `display` enum alone, which TikTok also
# reuses for other envelope-family announcements.
#
# (An earlier draft of this also wired up WebcastGoodyBagMessage/
# GoodyBagEvent, TikTok's other JumpPage.TREASURE_BOX-tagged message seen in
# some TikTokLive releases -- but the version actually pinned in this
# project's .venv (TikTokLiveProto v3 / TikTokLive 7.0.0) has no such class,
# so that path was dropped rather than shipped untested against a
# nonexistent import.)
_TREASURE_BOX_DISPLAY_TEXT_KEY = "pm_mt_treasure_box_sender_comment"


def is_treasure_box_envelope(event: Any) -> bool:
    """True only for the Treasure Box variant of EnvelopeEvent -- see the
    module-level Treasure Box comment above for why display_text.key (not
    the `display` enum) is the discriminator."""
    common = getattr(event, "common", None)
    display_text = getattr(common, "display_text", None) if common else None
    key = getattr(display_text, "key", None) if display_text else None
    return key == _TREASURE_BOX_DISPLAY_TEXT_KEY


def normalize_treasure_box_envelope(event: Any) -> tuple[str, str | None, str | None, dict]:
    """Only meaningful when is_treasure_box_envelope(event) is True -- reads
    envelope_info directly as a proto message (typed fields, no string
    parsing needed; the repr()-string appearance of this field in old debug
    captures was an artifact of that capture script's own serialization,
    not a limitation of the underlying data)."""
    common = getattr(event, "common", None)
    envelope_info = getattr(event, "envelope_info", None)
    send_user_id = getattr(envelope_info, "send_user_id", None) if envelope_info else None
    send_user_name = getattr(envelope_info, "send_user_name", None) if envelope_info else None
    payload = {
        "box_id": getattr(envelope_info, "envelope_id", None) if envelope_info else None,
        "coins": getattr(envelope_info, "diamond_count", None) if envelope_info else None,
        "winner_headcount": getattr(envelope_info, "people_count", None) if envelope_info else None,
        "open_at": getattr(envelope_info, "unpack_at", None) if envelope_info else None,
        "sent_at_ms": getattr(common, "create_time", None) if common else None,
    }
    user_id = str(send_user_id) if send_user_id is not None else None
    return "treasure_box", user_id, send_user_name, payload


def find_display_ids(raw_payload: dict) -> list[str]:
    """Extract candidate display_id values from an already-serialized event
    (see safe_serialize), in order of first appearance, deduplicated."""
    blob = json.dumps(raw_payload, ensure_ascii=False, default=str)
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in _DISPLAY_ID_PATTERN.findall(blob):
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def filter_battle_opponents(candidates: list[str], own_username: str) -> list[str]:
    """Drop null/zero placeholders and the monitored streamer's own handle
    (substring match, matching the reference implementation's behavior)."""
    own = (own_username or "").lower()
    return [
        c
        for c in candidates
        if c not in ("0", "None", "") and not (own and own in c.lower())
    ]
