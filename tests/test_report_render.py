import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.report.render import render_report


def make_aggregated(**stats_overrides):
    stats = {
        "duration_seconds": 3725,
        "max_viewers": 480,
        "avg_viewers": 320.4,
        "final_likes": 12345,
        "comment_count": 200,
        "total_diamonds": 5000,
        "unique_gifters": 12,
        "follow_count": 8,
        "unique_visitors": 90,
        "battle_opponent_count": 0,
    }
    stats.update(stats_overrides)
    return {
        "session": {"id": 1, "streamer_name": "テスト配信者", "started_at": "2026-08-24T11:00:00+00:00"},
        "basic_stats": stats,
        "timeseries": [],
        "comment_samples": [],
        "gift_ranking": [],
        "screenshot_path": None,
    }


def make_ai_sections(**overrides):
    sections = {
        "viewer_highlights": "視聴者数は開始10分で急増した。",
        "comment_trends": "ポジティブなコメントが多かった。",
        "visual_feedback": "画角はやや左に寄っている。",
        "next_stream_suggestions": ["開始時刻を20時にする(根拠: ギフトが20時台に集中)"],
    }
    sections.update(overrides)
    return sections


def test_render_report_includes_all_sections_and_summary():
    aggregated = make_aggregated()
    ai_sections = make_ai_sections()

    summary_json, md = render_report(aggregated, ai_sections)

    assert "# 配信分析レポート — テスト配信者 (2026-08-24)" in md
    assert "## サマリー" in md
    assert "1時間2分5秒" in md  # 3725s
    assert "320.4" in md
    assert "480" in md
    assert "## 数値のハイライト" in md
    assert "視聴者数は開始10分で急増した。" in md
    assert "## 次回配信への提案" in md
    assert "1. 開始時刻を20時にする" in md

    assert summary_json["basic_stats"]["max_viewers"] == 480
    assert summary_json["sections"] == ai_sections
    assert summary_json["generated_from_session_id"] == 1
    assert summary_json["used_winning_patterns"] is False


def test_render_report_records_used_winning_patterns_flag():
    summary_json, _md = render_report(make_aggregated(), make_ai_sections(), used_winning_patterns=True)
    assert summary_json["used_winning_patterns"] is True


def test_render_report_records_comment_curation_stats():
    stats = {
        "total_comments": 3000,
        "noise_excluded": 1200,
        "representative_count": 150,
        "category_counts_total": {"question": 20, "request": 10, "negative": 15, "other": 1755},
        "category_counts_representative": {"question": 20, "request": 10, "negative": 15, "other": 105},
    }
    summary_json, _md = render_report(make_aggregated(), make_ai_sections(), comment_curation_stats=stats)
    assert summary_json["comment_curation_stats"] == stats


def test_render_report_handles_missing_ai_section_gracefully():
    aggregated = make_aggregated()
    ai_sections = make_ai_sections()
    del ai_sections["comment_trends"]

    _summary, md = render_report(aggregated, ai_sections)

    assert "## コメント傾向" in md
    assert "(生成されませんでした)" in md


def test_render_report_shows_battle_count_only_when_present():
    aggregated_no_battle = make_aggregated(battle_opponent_count=0)
    _summary, md_no_battle = render_report(aggregated_no_battle, make_ai_sections())
    assert "バトル" not in md_no_battle

    aggregated_with_battle = make_aggregated(battle_opponent_count=2)
    _summary, md_with_battle = render_report(aggregated_with_battle, make_ai_sections())
    assert "バトル: 2回検知" in md_with_battle


def test_render_report_handles_unfinished_session_duration():
    aggregated = make_aggregated(duration_seconds=None)
    _summary, md = render_report(aggregated, make_ai_sections())
    assert "配信中(未終了)" in md
