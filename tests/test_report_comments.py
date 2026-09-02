import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.report.comments import (
    _categorize,
    _detect_spike_minutes,
    _is_noise,
    curate_comments,
)


def make_comment(second, text, nickname="user", minute=0, hour=0):
    return {
        "time": f"2026-01-01T{hour:02d}:{minute:02d}:{second:02d}+00:00",
        "nickname": nickname,
        "comment": text,
    }


def test_is_noise_flags_pure_emoji_and_bare_greetings():
    assert _is_noise("👏👏👏") is True
    assert _is_noise("こんにちは") is True
    assert _is_noise("こんにちは😊") is True  # greeting + decorative emoji is still just a greeting
    assert _is_noise("") is True
    assert _is_noise(None) is True


def test_is_noise_keeps_substantive_content():
    assert _is_noise("優しい") is False
    assert _is_noise("汗がハート💙") is False  # emoji stripped, real text remains
    assert _is_noise("💙meg💙") is False  # a nickname-like token remains after stripping


def test_categorize_detects_question_request_negative():
    assert _categorize("今日は何時からですか?") == "question"
    assert _categorize("次はダンスやってー") == "request"
    assert _categorize("うざい、帰れ") == "negative"
    assert _categorize("かわいい") == "other"


def test_categorize_negative_covers_technical_and_content_complaints():
    # These were the exact phrasings that fell through the original
    # keyword-only list (see synthetic-data verification).
    for text in ("声小さくて聞こえない", "画面暗すぎ", "音質悪い", "もう飽きた"):
        assert _categorize(text) == "negative", text


def test_categorize_negative_pattern_is_deliberately_over_inclusive():
    # Documented tradeoff, not a bug: broad recall over precision. The AI
    # is instructed (sections.py) to discard false positives like this one
    # at generation time rather than have layer 1 try to be precise here.
    assert _categorize("かわいすぎ") == "negative"


def test_detect_spike_minutes_finds_obvious_spike():
    comments = []
    # 10 quiet minutes with 1 comment each, one loud minute with 20.
    for m in range(10):
        comments.append(make_comment(0, "hi", minute=m))
    for s in range(20):
        comments.append(make_comment(s, "wow", minute=10))
    for m in range(11, 15):
        comments.append(make_comment(0, "hi", minute=m))

    spikes = _detect_spike_minutes(comments)
    assert any("00:10:00" in k for k in spikes)  # spike minute itself is flagged
    # adjacent minutes (09 and 11) should be pulled in for context
    assert any("00:09:00" in k for k in spikes)
    assert any("00:11:00" in k for k in spikes)


def test_detect_spike_minutes_returns_empty_for_too_few_buckets():
    comments = [make_comment(s, "hi", minute=0) for s in range(5)]
    assert _detect_spike_minutes(comments) == set()


def test_curate_comments_returns_everything_when_under_budget():
    comments = [make_comment(i, f"msg{i}", minute=0) for i in range(5)]
    result = curate_comments(comments, max_representative=150)
    assert len(result["representative"]) == 5


def test_curate_comments_drops_noise_even_when_under_budget():
    comments = [
        make_comment(0, "優しい", minute=0),
        make_comment(1, "👏👏👏", minute=0),
        make_comment(2, "こんにちは", minute=0),
    ]
    result = curate_comments(comments, max_representative=150)
    texts = [c["comment"] for c in result["representative"]]
    assert texts == ["優しい"]


def test_curate_comments_includes_every_rare_category_comment_uncapped():
    # 20 distinct questions -- more than the old 8-per-category floor ever
    # allowed through. All of them must survive now.
    comments = [
        make_comment(i, f"質問その{i}ですか?", minute=0, nickname=f"asker{i}") for i in range(20)
    ]
    # A flood of generic "other" comments competing for the same budget.
    for i in range(300):
        comments.append(make_comment(i % 60, "かわいい", minute=i // 60 % 60, nickname=f"u{i}"))

    result = curate_comments(comments, max_representative=50)
    question_count = sum(1 for c in result["representative"] if c["category"] == "question")
    assert question_count == 20  # every question included, budget or not


def test_curate_comments_respects_budget_and_guarantees_category_coverage():
    comments = []
    # A flood of generic "other" comments -- far more than the budget.
    for i in range(300):
        comments.append(make_comment(i % 60, "かわいい", minute=i // 60 % 60, nickname=f"u{i}"))
    # A handful of rare-category comments mixed in.
    comments.append(make_comment(5, "何時から配信ですか?", minute=1))
    comments.append(make_comment(6, "次回は歌ってー", minute=2))
    comments.append(make_comment(7, "うざいから帰れ", minute=3))

    result = curate_comments(comments, max_representative=50)
    representative = result["representative"]

    assert len(representative) <= 50
    categories_present = {c["category"] for c in representative}
    assert {"question", "request", "negative"}.issubset(categories_present)


def test_curate_comments_topic_buckets_cover_all_comments_regardless_of_sampling():
    comments = [make_comment(i, "かわいい", minute=0, nickname=f"u{i}") for i in range(10)]
    result = curate_comments(comments, max_representative=2)

    assert len(result["representative"]) == 2
    total_bucketed = sum(b["comment_count"] for b in result["topic_buckets"])
    assert total_bucketed == 10  # every comment is reflected in topic_buckets even though only 2 were sampled


def test_topic_buckets_report_repeated_text():
    comments = [make_comment(i, "スタート", minute=0, nickname=f"u{i}") for i in range(4)]
    comments.append(make_comment(5, "こんにちは", minute=0, nickname="unique"))

    result = curate_comments(comments, max_representative=150)
    bucket = result["topic_buckets"][0]
    assert bucket["comment_count"] == 5
    assert {"text": "スタート", "count": 4} in bucket["top_repeated"]


def test_curate_comments_handles_empty_input():
    result = curate_comments([])
    assert result["representative"] == []
    assert result["topic_buckets"] == []
    assert result["stats"] == {
        "total_comments": 0,
        "noise_excluded": 0,
        "representative_count": 0,
        "category_counts_total": {"question": 0, "request": 0, "negative": 0, "other": 0},
        "category_counts_representative": {"question": 0, "request": 0, "negative": 0, "other": 0},
    }


def test_curate_comments_stats_are_accurate():
    comments = [
        make_comment(0, "優しい", minute=0),  # other, kept
        make_comment(1, "👏👏👏", minute=0),  # noise
        make_comment(2, "こんにちは", minute=0),  # noise
        make_comment(3, "何時からですか?", minute=0),  # question
        make_comment(4, "歌ってー", minute=0),  # request
        make_comment(5, "うざい", minute=0),  # negative
    ]
    result = curate_comments(comments, max_representative=150)
    stats = result["stats"]

    assert stats["total_comments"] == 6
    assert stats["noise_excluded"] == 2
    assert stats["representative_count"] == 4
    assert stats["category_counts_total"] == {"question": 1, "request": 1, "negative": 1, "other": 1}
    # question/request/negative are never sampled, so total == representative for those
    assert stats["category_counts_representative"] == {"question": 1, "request": 1, "negative": 1, "other": 1}


def test_curate_comments_stats_show_other_sampling_ratio_when_over_budget():
    comments = [make_comment(i % 60, "かわいい", minute=i // 60, nickname=f"u{i}") for i in range(300)]
    result = curate_comments(comments, max_representative=50)
    stats = result["stats"]

    assert stats["total_comments"] == 300
    assert stats["noise_excluded"] == 0
    assert stats["representative_count"] == 50
    assert stats["category_counts_total"]["other"] == 300
    assert stats["category_counts_representative"]["other"] == 50  # sampled down to budget
