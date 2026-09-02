"""Renders the final Markdown report (recommendation_md) from Python-computed
basic stats + Claude's structured section output. This is the "template"
half of the prompt/template split — change section order or wording here
without touching prompt.py.
"""
from .sections import REPORT_SECTIONS


def render_report(
    aggregated: dict,
    ai_sections: dict,
    used_winning_patterns: bool = False,
    usage: dict | None = None,
    comment_curation_stats: dict | None = None,
) -> tuple[dict, str]:
    """Returns (summary_json, recommendation_md) ready for db.insert_report."""
    session = aggregated["session"]
    stats = aggregated["basic_stats"]

    date_label = (session["started_at"] or "")[:10] or "?"
    lines = [
        f"# 配信分析レポート — {session['streamer_name']} ({date_label})",
        "",
        "## サマリー",
        f"- 配信時間: {_format_duration(stats['duration_seconds'])}",
        f"- 平均視聴者数: {_fmt(stats['avg_viewers'])} / 最高視聴者数: {_fmt(stats['max_viewers'])}",
        f"- 総ギフト: {_fmt(stats['total_diamonds'])}ダイヤ({_fmt(stats['unique_gifters'])}人) "
        f"/ コメント数: {_fmt(stats['comment_count'])}件",
        f"- フォロー: {_fmt(stats['follow_count'])}件 / 入室ユーザー数: {_fmt(stats['unique_visitors'])}人",
    ]
    if stats.get("battle_opponent_count"):
        lines.append(f"- バトル: {stats['battle_opponent_count']}回検知")
    lines.append("")

    for section in REPORT_SECTIONS:
        lines.append(f"## {section['title']}")
        value = ai_sections.get(section["key"])
        if isinstance(value, list):
            lines.extend(f"{i + 1}. {item}" for i, item in enumerate(value))
        elif value:
            lines.append(str(value))
        else:
            lines.append("(生成されませんでした)")
        lines.append("")

    recommendation_md = "\n".join(lines).rstrip() + "\n"

    summary_json = {
        "basic_stats": stats,
        "sections": ai_sections,
        "generated_from_session_id": session["id"],
        "used_winning_patterns": used_winning_patterns,
        "usage": usage,
        "comment_curation_stats": comment_curation_stats,
    }
    return summary_json, recommendation_md


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{int(value)}" if value.is_integer() else f"{value:.1f}"
    return str(value)


def _format_duration(seconds) -> str:
    if seconds is None:
        return "配信中(未終了)"
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}時間{minutes}分{secs}秒"
