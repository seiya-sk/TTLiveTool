"""系統2: 進捗通知の集計。

窓(1時間)と、グループに属するライバーの集合を受け取り、その窓の進捗を
まとめる。送信は行わない -- 数値の正しさを送信と切り離して検証できる
ようにするため。

指標は5つ: 配信本数(継続中を含む) / 配信時間(延べ) / 最大同接 /
ダイヤ / 新規フォロー。like と comment は意図的に出さない(実測で
like 2217件・comment 1242件/50分と多く、事務所が見る進捗としては
ノイズになるため)。

ダイヤの集計は report/data.py の重複排除ロジックをそのまま使う。
streaking の途中経過・同一ギフトの重複配信・log_id の再利用という3つの
罠があり、ダッシュボードの表示と食い違わせないために実装を1箇所に保つ。
"""
import json
from datetime import datetime, timedelta, timezone

from ..report import data as report_data

JST = timezone(timedelta(hours=9))

# ライバーがこの人数を超えるグループは、1通が長くなりすぎるので
# ライバー別明細を省いてサマリのみにする。
DETAIL_MAX_STREAMERS = 10

# 窓境界で重複排除が分断されないよう、内側の取得範囲に持たせる余裕。
# 重複は数秒以内に届くので60秒で十分吸収できる(tests/test_deduped_gifts_window.py)。
GIFT_WINDOW_PAD_SEC = 60


def hour_window(now: datetime | None = None) -> tuple[str, str]:
    """直前の1時間(HH-1:00 〜 HH:00)をUTCのISO文字列で返す。

    窓はJSTの時境界に揃える(このプロジェクトの表示がJST基準のため)。
    DBの occurred_at / started_at は +00:00 付きUTC ISO なので、辞書順
    比較がそのまま時刻比較として成立する。
    """
    now = (now or datetime.now(timezone.utc)).astimezone(JST)
    end_jst = now.replace(minute=0, second=0, microsecond=0)
    start_jst = end_jst - timedelta(hours=1)
    return (
        start_jst.astimezone(timezone.utc).isoformat(),
        end_jst.astimezone(timezone.utc).isoformat(),
    )


def window_label(start_utc_iso: str, end_utc_iso: str) -> str:
    s = datetime.fromisoformat(start_utc_iso).astimezone(JST)
    e = datetime.fromisoformat(end_utc_iso).astimezone(JST)
    return f"{s:%m/%d %H:%M}–{e:%H:%M}"


def _shift(iso: str, seconds: int) -> str:
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat()


def _overlap_seconds(started_at: str, ended_at: str | None, start: str, end: str, now_iso: str) -> float:
    """配信と窓の重なり秒数。配信が窓をまたぐ場合に正しく按分される。
    ended_at が NULL(継続中)なら現在時刻で打ち切る。"""
    s = datetime.fromisoformat(max(started_at, start))
    stop_candidate = ended_at or now_iso
    e = datetime.fromisoformat(min(stop_candidate, end))
    return max(0.0, (e - s).total_seconds())


def collect_group_progress(conn, streamer_ids: list[int], start: str, end: str, now: datetime | None = None) -> dict:
    """グループの窓内進捗。streamer_ids が空なら空の結果を返す。"""
    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    empty = {
        "streamers": [], "streamer_count": 0, "session_count": 0,
        "live_count": 0, "total_seconds": 0.0,
        "total_diamonds": 0, "total_follows": 0, "max_viewers": 0,
    }
    if not streamer_ids:
        return empty

    placeholders = ",".join("?" * len(streamer_ids))
    # 窓と重なる配信: 窓終了より前に始まり、窓開始より後に終わった(または継続中)
    sessions = conn.execute(
        f"""
        SELECT s.id, s.streamer_id, st.name, s.started_at, s.ended_at
        FROM live_sessions s
        JOIN streamers st ON st.id = s.streamer_id
        WHERE s.streamer_id IN ({placeholders})
          AND s.started_at < ?
          AND (s.ended_at IS NULL OR s.ended_at > ?)
        ORDER BY s.streamer_id, s.started_at
        """,
        (*streamer_ids, end, start),
    ).fetchall()

    per_streamer: dict[int, dict] = {}
    for session_id, streamer_id, name, started_at, ended_at in sessions:
        entry = per_streamer.setdefault(streamer_id, {
            "streamer_id": streamer_id, "name": name, "sessions": 0, "seconds": 0.0,
            "live_now": False, "max_viewers": 0, "diamonds": 0, "follows": 0,
        })
        entry["sessions"] += 1
        entry["seconds"] += _overlap_seconds(started_at, ended_at, start, end, now_iso)
        if ended_at is None:
            entry["live_now"] = True

        mv = conn.execute(
            "SELECT MAX(CAST(json_extract(payload,'$.viewer_count') AS INTEGER)) FROM live_events "
            "WHERE live_session_id=? AND event_type='viewer_count' AND occurred_at >= ? AND occurred_at < ?",
            (session_id, start, end),
        ).fetchone()[0]
        entry["max_viewers"] = max(entry["max_viewers"], mv or 0)

        entry["follows"] += conn.execute(
            "SELECT COUNT(*) FROM live_events "
            "WHERE live_session_id=? AND event_type='follow' AND occurred_at >= ? AND occurred_at < ?",
            (session_id, start, end),
        ).fetchone()[0]

        diamonds = conn.execute(
            f"SELECT SUM(diamond_value) FROM ({report_data.deduped_gifts_subquery(windowed=True)})",
            (session_id, _shift(start, -GIFT_WINDOW_PAD_SEC), _shift(end, GIFT_WINDOW_PAD_SEC), start, end),
        ).fetchone()[0]
        entry["diamonds"] += diamonds or 0

    rows = sorted(per_streamer.values(), key=lambda r: (-r["diamonds"], -r["seconds"], r["name"] or ""))
    return {
        "streamers": rows,
        "streamer_count": len(rows),
        "session_count": sum(r["sessions"] for r in rows),
        "live_count": sum(1 for r in rows if r["live_now"]),
        "total_seconds": sum(r["seconds"] for r in rows),
        "total_diamonds": sum(r["diamonds"] for r in rows),
        "total_follows": sum(r["follows"] for r in rows),
        "max_viewers": max((r["max_viewers"] for r in rows), default=0),
    }


def format_duration(seconds: float) -> str:
    total_min = int(seconds // 60)
    h, m = divmod(total_min, 60)
    return f"{h}時間{m}分" if h else f"{m}分"


def format_digest(group_name: str, progress: dict, start: str, end: str,
                  detail_max: int = DETAIL_MAX_STREAMERS) -> str:
    """Chatwork の [info] ブロックを組み立てる。人数が detail_max を超える
    グループはライバー別明細を省く(1通が長くなりすぎるため)。"""
    label = window_label(start, end)
    live_note = f"(うち継続中{progress['live_count']}本)" if progress["live_count"] else ""
    lines = [
        f"配信: {progress['streamer_count']}人 / {progress['session_count']}本 {live_note}".rstrip(),
        f"延べ配信時間: {format_duration(progress['total_seconds'])}",
        f"最大同接: {progress['max_viewers']}  ダイヤ: {progress['total_diamonds']:,}  "
        f"新規フォロー: {progress['total_follows']}",
    ]
    rows = progress["streamers"]
    if not rows:
        lines.append("\nこの時間帯の配信はありませんでした。")
    elif len(rows) <= detail_max:
        lines.append("")
        for r in rows:
            live = "(継続中)" if r["live_now"] else ""
            lines.append(
                f"{r['name']}  {r['sessions']}本 {format_duration(r['seconds'])}{live}  "
                f"同接{r['max_viewers']}  💎{r['diamonds']:,}  ＋{r['follows']}"
            )
    else:
        lines.append(f"\n※ 対象{len(rows)}人のためライバー別明細は省略しています")
    return f"[info][title]{group_name} 進捗 {label}[/title]" + "\n".join(lines) + "[/info]"
