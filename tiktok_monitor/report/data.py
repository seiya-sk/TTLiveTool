"""Aggregates live_events/live_screenshots for one session into the compact
shape fed to the Claude prompt. Raw logs are never sent directly (design doc
5.1: pre-aggregate first, curate comments, to avoid blowing up tokens).
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from . import comments as comments_module

# occurred_at is stored in UTC; Claude only ever sees the times below (never
# occurred_at directly), and without this it happily cites UTC clock times
# in the generated report prose (e.g. "15:20頃") that don't match the
# streamer's actual JST-local stream. Converting once here, before anything
# is handed to comments.curate_comments or the prompt, means every
# downstream consumer (spike/topic-bucket grouping, the prompt JSON) works
# in JST without needing its own conversion -- bucketing correctness is
# unaffected since it's just a constant shift applied uniformly.
_JST = timezone(timedelta(hours=9))


def _to_jst_iso(utc_iso: str) -> str:
    return datetime.fromisoformat(utc_iso).astimezone(_JST).isoformat()

# A streaking gift's repeat_count keeps climbing on every tick; only the
# final (streaking=false) event per combo carries the settled total, so
# summing anything else double counts. Mirrors GiftEvent.value in
# tiktok_monitor/events.py and dashboard/src/lib/queries.ts.
#
# Separately: TikTok sometimes delivers the SAME settled gift as multiple
# distinct webcast messages (confirmed on real data -- identical user,
# timestamp, gift, and diamond value, but a different common.msg_id each
# time), all sharing one log_id. A naive "GROUP BY log_id" fix is not safe
# on its own, though: also confirmed on real data, TikTok can *reuse* the
# same log_id for two genuinely separate gifts from the same user a few
# seconds apart (different diamond_count each). Grouping by (log_id,
# streaking, diamond_count, repeat_count) together handles both: true
# duplicates share all four and collapse to one row via MAX(); two
# reused-log_id-but-different-value gifts differ on diamond_count/
# repeat_count and land in separate groups, so both are still counted.
# id is the fallback grouping key for the never-observed-but-defend-anyway
# case where log_id is blank. Confirmed against real data: a 2,923-row/
# 7-hour session had 5 true-duplicate groups inflating the total by ~5%,
# and separately 5 reused-log_id-different-gift groups that a naive
# log_id-only GROUP BY would have wrongly collapsed (losing ~20 diamonds).
#
# log_id is read from `payload`, not raw_payload: events.py's
# normalize_gift promotes it into the curated payload specifically so this
# query never has to join back to the (separately stored, much larger)
# raw_payload table -- see db.py's live_events comment.
# 重複排除の本体はここ1箇所だけ。時間窓ありの集計(進捗通知)も、窓なしの
# 集計(レポート/ダッシュボード)も、同じ GROUP BY と同じ diamond_value の
# 式を通す -- ギフト集計の正しさは微妙なので、条件分岐で式が枝分かれする
# ことを避ける。{time_filter} には窓ありのときだけ occurred_at の絞り込みが
# 入り、窓なしのときは空文字なので、従来のSQLと完全に同一の文字列になる。
_DEDUPED_GIFTS_CORE = """
    SELECT
        MAX(user_id) as user_id,
        MAX(user_nickname) as user_nickname,
        MAX(occurred_at) as occurred_at,
        MAX(CASE WHEN json_extract(payload,'$.streaking') = 0
            THEN COALESCE(json_extract(payload,'$.diamond_count'),0) * COALESCE(json_extract(payload,'$.repeat_count'),1)
            ELSE 0 END) as diamond_value
    FROM live_events
    WHERE live_session_id = ? AND event_type = 'gift'{time_filter}
    GROUP BY
        COALESCE(NULLIF(json_extract(payload,'$.log_id'), ''), id),
        json_extract(payload,'$.streaking'),
        json_extract(payload,'$.diamond_count'),
        json_extract(payload,'$.repeat_count')
"""

_DEDUPED_GIFTS_SUBQUERY = _DEDUPED_GIFTS_CORE.format(time_filter="")


def deduped_gifts_subquery(windowed: bool = False) -> str:
    """重複排除済みギフトのサブクエリ。

    windowed=False (既定): 従来と完全に同一。プレースホルダは
        (live_session_id,) の1個。既存の呼び出しは何も変わらない。

    windowed=True: 時間窓で絞り込む。プレースホルダは5個で、順序は
        (live_session_id, 内側開始, 内側終了, 窓開始, 窓終了)。

    窓ありのとき内側と外側で範囲を分けているのは、重複排除のGROUP BYが
    「同じ log_id を持つ行がすべて同じクエリ結果に入っていること」を前提
    にしているため。窓の境界ぴったりで重複が分断されると、前後の窓で1回
    ずつ数えられてしまう。そこで内側は前後に余裕(既定60秒、呼び出し側が
    決める)を持たせて重複をまとめきり、外側でグループ代表の occurred_at
    が本来の窓に入るものだけを残す。

    注意: このロジックは dashboard/src/lib/queries.ts の
    DEDUPED_GIFTS_SUBQUERY と対になっている。windowed=False の出力は
    従来と同一文字列なので、TS側は変更不要 -- ただし GROUP BY や
    diamond_value の式そのものを変えるときは、従来どおり両方を直すこと。
    """
    if not windowed:
        return _DEDUPED_GIFTS_SUBQUERY
    inner = _DEDUPED_GIFTS_CORE.format(
        time_filter="\n        AND occurred_at >= ? AND occurred_at < ?"
    )
    return f"SELECT * FROM ({inner}) WHERE occurred_at >= ? AND occurred_at < ?"


def aggregate_session_data(conn: sqlite3.Connection, live_session_id: int, max_comments: int = 150) -> dict:
    session = _get_session(conn, live_session_id)
    curated_comments = comments_module.curate_comments(
        _fetch_all_comments(conn, live_session_id), max_representative=max_comments
    )
    return {
        "session": session,
        "basic_stats": _compute_basic_stats(conn, live_session_id, session),
        "timeseries": _get_timeseries(conn, live_session_id),
        "comment_samples": curated_comments["representative"],
        "comment_topic_buckets": curated_comments["topic_buckets"],
        "comment_curation_stats": curated_comments["stats"],
        "gift_ranking": _get_gift_ranking(conn, live_session_id),
        "screenshot_path": _get_latest_screenshot_path(conn, live_session_id),
    }


def _get_session(conn: sqlite3.Connection, live_session_id: int) -> dict:
    row = conn.execute(
        """
        SELECT ls.id, s.name, s.tiktok_account_id, ls.title, ls.started_at, ls.ended_at, ls.status, ls.end_detection_type
        FROM live_sessions ls JOIN streamers s ON s.id = ls.streamer_id
        WHERE ls.id = ?
        """,
        (live_session_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"live_session_id={live_session_id} not found")
    return {
        "id": row[0],
        "streamer_name": row[1],
        "tiktok_account_id": row[2],
        "title": row[3],
        "started_at": row[4],
        "ended_at": row[5],
        "status": row[6],
        "end_detection_type": row[7],
    }


def _scalar(conn: sqlite3.Connection, query: str, live_session_id: int, default=None):
    row = conn.execute(query, (live_session_id,)).fetchone()
    return row[0] if row and row[0] is not None else default


def _compute_basic_stats(conn: sqlite3.Connection, live_session_id: int, session: dict) -> dict:
    max_viewers = _scalar(
        conn,
        "SELECT MAX(CAST(json_extract(payload,'$.viewer_count') AS INTEGER)) FROM live_events "
        "WHERE live_session_id=? AND event_type='viewer_count'",
        live_session_id,
    )
    avg_viewers = _scalar(
        conn,
        "SELECT AVG(CAST(json_extract(payload,'$.viewer_count') AS INTEGER)) FROM live_events "
        "WHERE live_session_id=? AND event_type='viewer_count'",
        live_session_id,
    )
    final_likes = _scalar(
        conn,
        "SELECT CAST(json_extract(payload,'$.total_likes') AS INTEGER) FROM live_events "
        "WHERE live_session_id=? AND event_type='like' ORDER BY occurred_at DESC LIMIT 1",
        live_session_id,
    )
    comment_count = _scalar(
        conn, "SELECT COUNT(*) FROM live_events WHERE live_session_id=? AND event_type='comment'", live_session_id, 0
    )
    total_diamonds = _scalar(
        conn,
        f"SELECT SUM(diamond_value) FROM ({_DEDUPED_GIFTS_SUBQUERY})",
        live_session_id,
        0,
    )
    unique_gifters = _scalar(
        conn,
        "SELECT COUNT(DISTINCT user_id) FROM live_events WHERE live_session_id=? AND event_type='gift'",
        live_session_id,
        0,
    )
    follow_count = _scalar(
        conn, "SELECT COUNT(*) FROM live_events WHERE live_session_id=? AND event_type='follow'", live_session_id, 0
    )
    unique_visitors = _scalar(
        conn,
        "SELECT COUNT(DISTINCT user_id) FROM live_events WHERE live_session_id=? AND event_type='room_enter'",
        live_session_id,
        0,
    )
    battle_opponent_count = _scalar(
        conn,
        "SELECT COUNT(DISTINCT user_id) FROM live_events WHERE live_session_id=? AND event_type='battle_opponent'",
        live_session_id,
        0,
    )

    duration_seconds = None
    if session["ended_at"]:
        started = datetime.fromisoformat(session["started_at"])
        ended = datetime.fromisoformat(session["ended_at"])
        duration_seconds = max(0, round((ended - started).total_seconds()))

    return {
        "duration_seconds": duration_seconds,
        "max_viewers": max_viewers,
        "avg_viewers": round(avg_viewers, 1) if avg_viewers is not None else None,
        "final_likes": final_likes,
        "comment_count": comment_count,
        "total_diamonds": total_diamonds,
        "unique_gifters": unique_gifters,
        "follow_count": follow_count,
        "unique_visitors": unique_visitors,
        "battle_opponent_count": battle_opponent_count,
    }


def _get_timeseries(conn: sqlite3.Connection, live_session_id: int) -> list[dict]:
    # '+9 hours' shifts the UTC occurred_at to JST before bucketing; the
    # literal "+09:00" in the format string (not a strftime specifier,
    # passed through as-is) self-labels the result so it reads correctly
    # even out of context. See _to_jst_iso's docstring for why this matters.
    viewer_rows = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:00+09:00', occurred_at, '+9 hours') as minute, "
        "AVG(CAST(json_extract(payload,'$.viewer_count') AS INTEGER)) as v "
        "FROM live_events WHERE live_session_id=? AND event_type='viewer_count' GROUP BY minute",
        (live_session_id,),
    ).fetchall()
    comment_rows = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:00+09:00', occurred_at, '+9 hours') as minute, COUNT(*) as c "
        "FROM live_events WHERE live_session_id=? AND event_type='comment' GROUP BY minute",
        (live_session_id,),
    ).fetchall()
    gift_rows = conn.execute(
        f"SELECT strftime('%Y-%m-%dT%H:%M:00+09:00', occurred_at, '+9 hours') as minute, SUM(diamond_value) as d "
        f"FROM ({_DEDUPED_GIFTS_SUBQUERY}) GROUP BY minute",
        (live_session_id,),
    ).fetchall()

    merged: dict[str, dict] = {}
    for minute, v in viewer_rows:
        merged.setdefault(minute, {})["viewers"] = round(v) if v is not None else None
    for minute, c in comment_rows:
        merged.setdefault(minute, {})["comments"] = c
    for minute, d in gift_rows:
        merged.setdefault(minute, {})["diamonds"] = d

    return [{"minute": minute, **values} for minute, values in sorted(merged.items())]


def _fetch_all_comments(conn: sqlite3.Connection, live_session_id: int) -> list[dict]:
    """Every comment, chronological. Curation (spike detection, noise
    filtering, category coverage) happens in comments.curate_comments, which
    needs the full list to work from -- nothing is capped at the SQL level."""
    rows = conn.execute(
        "SELECT occurred_at, user_nickname, json_extract(payload,'$.comment') as comment "
        "FROM live_events WHERE live_session_id=? AND event_type='comment' ORDER BY occurred_at",
        (live_session_id,),
    ).fetchall()
    return [{"time": _to_jst_iso(r[0]), "nickname": r[1], "comment": r[2]} for r in rows if r[2]]


def _get_gift_ranking(conn: sqlite3.Connection, live_session_id: int, max_n: int = 15) -> list[dict]:
    rows = conn.execute(
        f"SELECT user_nickname, SUM(diamond_value) as total_diamonds, COUNT(*) as gift_events "
        f"FROM ({_DEDUPED_GIFTS_SUBQUERY}) GROUP BY user_id ORDER BY total_diamonds DESC LIMIT ?",
        (live_session_id, max_n),
    ).fetchall()
    return [{"nickname": r[0], "total_diamonds": r[1], "gift_events": r[2]} for r in rows]


def _get_latest_screenshot_path(conn: sqlite3.Connection, live_session_id: int) -> str | None:
    row = conn.execute(
        "SELECT image_path FROM live_screenshots WHERE live_session_id=? ORDER BY captured_at DESC LIMIT 1",
        (live_session_id,),
    ).fetchone()
    return row[0] if row else None
