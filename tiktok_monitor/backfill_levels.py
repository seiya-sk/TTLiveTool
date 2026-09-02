"""One-off backfill: recompute gifter_level/member_level for already-stored
live_events rows using the corrected badge-scanning logic (see events.py's
_badge_level docstring -- TikTokLive's own gifter_level/member_level
properties read a stale badge.badge_scene/log_extra field pair that doesn't
match the actually-installed TikTokLiveProto schema, so every previously
recorded row has these as None even where TikTok did send level data).
raw_payload keeps the full original event, so this re-derives the two
fields from it in place; nothing needs to be re-fetched from TikTok. Safe
to re-run: each row is recomputed fresh from raw_payload, not accumulated.

raw_payload lives in live_event_raw_payloads (joined in here), not on
live_events itself -- see db.py's live_events comment for why.

Usage: python -m tiktok_monitor.backfill_levels [--db-path PATH] [--dry-run]
"""
import argparse
import json
import logging
import sqlite3

from . import db

logger = logging.getLogger(__name__)

_BADGE_SCENE_USER_GRADE = 8
_BADGE_SCENE_FANS = 10

_TARGET_EVENT_TYPES = ("gift", "comment", "room_enter", "follow")


def _badge_level_from_raw(user: dict | None, target_scene: int) -> int | None:
    if not user:
        return None
    for badge in user.get("badge_list") or []:
        if not isinstance(badge, dict):
            continue
        scene = badge.get("scene_type")
        if scene is None:
            continue
        try:
            if int(scene) != target_scene:
                continue
        except (TypeError, ValueError):
            continue
        log_extra = badge.get("privilege_log_extra") or {}
        level = log_extra.get("level")
        try:
            return int(level) if level is not None else None
        except (TypeError, ValueError):
            continue
    return None


def recompute_levels(raw_payload: dict) -> tuple[int | None, int | None]:
    user = raw_payload.get("user") if isinstance(raw_payload, dict) else None
    gifter_level = _badge_level_from_raw(user, _BADGE_SCENE_USER_GRADE)
    if gifter_level is None and user:
        pay_grade = user.get("pay_grade") or {}
        raw = pay_grade.get("level")
        try:
            gifter_level = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            gifter_level = None
    member_level = _badge_level_from_raw(user, _BADGE_SCENE_FANS)
    return gifter_level, member_level


def backfill(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    placeholders = ",".join("?" for _ in _TARGET_EVENT_TYPES)
    rows = conn.execute(
        f"""
        SELECT e.id, e.event_type, e.payload, r.raw_payload
        FROM live_events e
        JOIN live_event_raw_payloads r ON r.live_event_id = e.id
        WHERE e.event_type IN ({placeholders})
        """,
        _TARGET_EVENT_TYPES,
    ).fetchall()

    updated = 0
    for row_id, event_type, payload_json, raw_payload_json in rows:
        payload = json.loads(payload_json) if payload_json else {}
        raw_payload = json.loads(raw_payload_json) if raw_payload_json else {}
        gifter_level, member_level = recompute_levels(raw_payload)

        changed = False
        if "gifter_level" in payload and payload.get("gifter_level") != gifter_level:
            payload["gifter_level"] = gifter_level
            changed = True
        if event_type == "comment" and payload.get("member_level") != member_level:
            payload["member_level"] = member_level
            changed = True

        if changed:
            updated += 1
            if not dry_run:
                conn.execute(
                    "UPDATE live_events SET payload = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False, default=str), row_id),
                )

    if not dry_run:
        conn.commit()
    return {"scanned": len(rows), "updated": updated}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="data/tts_live_tool.db")
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing changes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    conn = db.connect(args.db_path)
    try:
        result = backfill(conn, dry_run=args.dry_run)
        logger.info(
            "%sscanned %d event(s), updated %d",
            "[dry-run] " if args.dry_run else "",
            result["scanned"],
            result["updated"],
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
