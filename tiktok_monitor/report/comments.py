"""Layer 1 comment curation (design doc 5.1): every comment is considered in
code, then condensed into two complementary views instead of one flat
even-spaced sample, so long streams don't silently drop important comments:

- "representative": a curated excerpt list — spike-aware (busier minutes get
  denser sampling), noise-filtered (pure emoji/stamp spam and bare greetings
  dropped), and category-complete (every question/request/negative reaction
  is included in full; only the generic "other" bucket is sampled to fit
  the budget). Measured on a 2,758-comment synthetic stream: guaranteeing
  question/request/negative in full costs ~150 tokens more than an 8-per-
  category floor did, out of ~29k total -- capping them was buying
  negligible savings at the cost of real misses.
- "topic_buckets": a per-N-minute fingerprint covering the ENTIRE comment
  history regardless of what made it into the representative list, so no
  time window is completely invisible to the model

Deliberately not real NLP/sentiment analysis. Categorization (question/
request/negative/other) is a cheap, intentionally over-inclusive keyword and
pattern match -- its job is recall (get candidates in front of the model),
not precision. Final judgment on what a "negative"-tagged comment is
actually complaining about, and whether it's a false positive at all, is
left to the AI at generation time (see sections.py's comment_trends
instruction). Easy to swap the heuristics out later without touching
callers -- data.py only depends on curate_comments' return shape.
"""
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime

SPIKE_BUCKET_MINUTES = 1
TOPIC_BUCKET_MINUTES = 5
SPIKE_SHARE = 0.4  # share of the post-question/request/negative budget reserved for spike-window "other" comments
SPIKE_MIN_BUCKETS = 5  # too few buckets to make mean/stdev spike detection meaningful below this
_RARE_CATEGORIES = ("question", "request", "negative")  # always included in full, never sampled

_QUESTION_MARKERS = ("?", "？", "ですか", "ますか", "かな", "教えて")
_REQUEST_MARKERS = ("してー", "て欲しい", "てほしい", "お願い", "やって", "見せて", "欲しい", "てー")
_NEGATIVE_MARKERS = (
    "最悪", "つまらない", "うざい", "帰れ", "嫌い", "きらい", "やめて", "うるさい", "しつこい",
    "聞こえない", "見えない", "暗すぎ", "悪い", "飽きた", "遅い", "止まった", "微妙", "きつい",
)
# Deliberately broad, best-effort patterns for negation/excessive-degree
# constructions common in complaints ("うるさすぎる", "全然聞こえない"). This is
# meant to over-match: layer 1's job is to surface candidates cheaply, not
# rule on them precisely. Comments this flags as "negative" are handed to
# the AI with an instruction to interpret what (if anything) they're
# actually complaining about and to silently disregard ones that turn out
# to be harmless (e.g. "かわいすぎ" is enthusiasm, not a complaint) -- see
# the comment_trends instruction in sections.py.
_NEGATIVE_PATTERNS = (
    re.compile(r"すぎ"),
    re.compile(r"ない[!！。、]?$"),
)
_GREETING_ONLY = {
    "こんにちは", "こんばんは", "おはよう", "おはようございます", "こんにちわ",
    "おつかれ", "おつです", "おつかれさま", "はじめまして",
}
# Not an exhaustive emoji range, just wide enough for what actually shows up
# in TikTok live chat (pictographs, dingbats/misc symbols, flags, ZWJ joins,
# variation selectors). Built from numeric codepoints via chr() rather than
# literal glyphs or \u escapes in a character-class string -- both are easy
# to mis-type/mis-encode in a way that silently changes which codepoints are
# matched, which is exactly the class of bug this module exists to avoid.
_EMOJI_CODEPOINT_RANGES = [
    (0x1F1E6, 0x1FAFF),  # regional indicators through extended pictographs
    (0x2600, 0x27BF),  # misc symbols + dingbats
    (0x2B00, 0x2BFF),  # misc symbols and arrows
]
_EMOJI_SINGLE_CODEPOINTS = [0xFE0E, 0xFE0F, 0x200D]  # variation selectors, ZWJ


def _build_emoji_pattern() -> re.Pattern:
    parts = [f"{chr(lo)}-{chr(hi)}" for lo, hi in _EMOJI_CODEPOINT_RANGES]
    parts.append("".join(chr(cp) for cp in _EMOJI_SINGLE_CODEPOINTS))
    return re.compile("[" + "".join(parts) + "]+")


_EMOJI_PATTERN = _build_emoji_pattern()


_CATEGORY_KEYS = ("question", "request", "negative", "other")


def _empty_category_counts() -> dict[str, int]:
    return {k: 0 for k in _CATEGORY_KEYS}


def curate_comments(comments: list[dict], max_representative: int = 150) -> dict:
    """comments: chronological list of {"time": iso, "nickname": str, "comment": str}.
    Returns {"representative": [...], "topic_buckets": [...], "stats": {...}}.

    stats exists so real-live verification can quantify whether anything is
    being missed: total_comments/noise_excluded/representative_count plus a
    category breakdown of both the full non-noise candidate pool and of
    what actually made it into representative (question/request/negative
    should always match 1:1 between the two -- only "other" is sampled)."""
    if not comments:
        return {
            "representative": [],
            "topic_buckets": [],
            "stats": {
                "total_comments": 0,
                "noise_excluded": 0,
                "representative_count": 0,
                "category_counts_total": _empty_category_counts(),
                "category_counts_representative": _empty_category_counts(),
            },
        }

    spike_minute_keys = _detect_spike_minutes(comments)

    enriched = []
    for c in comments:
        text = c.get("comment") or ""
        enriched.append(
            {
                **c,
                "_noise": _is_noise(text),
                "_category": _categorize(text),
                "_in_spike": _minute_bucket(c["time"], SPIKE_BUCKET_MINUTES) in spike_minute_keys,
            }
        )

    candidates = [c for c in enriched if not c["_noise"]]
    selected = _select_with_budget(candidates, max_representative)

    representative = [
        {"time": c["time"], "nickname": c["nickname"], "comment": c["comment"], "category": c["_category"]}
        for c in selected
    ]

    category_counts_total = _empty_category_counts()
    for c in candidates:
        category_counts_total[c["_category"]] += 1
    category_counts_representative = _empty_category_counts()
    for c in representative:
        category_counts_representative[c["category"]] += 1

    stats = {
        "total_comments": len(comments),
        "noise_excluded": len(comments) - len(candidates),
        "representative_count": len(representative),
        "category_counts_total": category_counts_total,
        "category_counts_representative": category_counts_representative,
    }

    return {"representative": representative, "topic_buckets": _summarize_topic_buckets(comments), "stats": stats}


def _minute_bucket(iso_time: str, bucket_minutes: int) -> str:
    dt = datetime.fromisoformat(iso_time)
    floored = (dt.minute // bucket_minutes) * bucket_minutes
    return dt.replace(minute=floored, second=0, microsecond=0).isoformat()


def _is_noise(comment: str) -> bool:
    stripped = (comment or "").strip()
    if not stripped:
        return True
    text_only = _EMOJI_PATTERN.sub("", stripped).strip()
    if not text_only:
        return True  # pure emoji/stamp spam
    if text_only in _GREETING_ONLY:
        return True  # bare greeting, decorative emoji or not
    return False


def _categorize(comment: str) -> str:
    if any(marker in comment for marker in _QUESTION_MARKERS):
        return "question"
    if any(marker in comment for marker in _REQUEST_MARKERS):
        return "request"
    if any(marker in comment for marker in _NEGATIVE_MARKERS):
        return "negative"
    # Patterns run against the emoji-stripped text so a trailing decorative
    # emoji (e.g. "聞こえない😢") doesn't defeat the end-anchored "ない" check.
    text_only = _EMOJI_PATTERN.sub("", comment).strip()
    if any(pattern.search(text_only) for pattern in _NEGATIVE_PATTERNS):
        return "negative"
    return "other"


def _detect_spike_minutes(comments: list[dict]) -> set[str]:
    per_minute: dict[str, int] = defaultdict(int)
    for c in comments:
        per_minute[_minute_bucket(c["time"], SPIKE_BUCKET_MINUTES)] += 1

    if len(per_minute) < SPIKE_MIN_BUCKETS:
        return set()

    values = list(per_minute.values())
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return set()

    threshold = mean + 1.5 * stdev
    spikes = {key for key, count in per_minute.items() if count >= threshold}

    # thicken: pull in the minute immediately before/after each spike too,
    # so we get some run-up/cool-down context, not just the single peak minute
    sorted_keys = sorted(per_minute.keys())
    expanded = set(spikes)
    for key in spikes:
        idx = sorted_keys.index(key)
        if idx > 0:
            expanded.add(sorted_keys[idx - 1])
        if idx < len(sorted_keys) - 1:
            expanded.add(sorted_keys[idx + 1])
    return expanded


def _even_sample(items: list, n: int) -> list:
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    indices = sorted({int(i * step) for i in range(n)})
    return [items[i] for i in indices]


def _select_with_budget(candidates: list[dict], budget: int) -> list[dict]:
    """question/request/negative are always included in full -- no cap.
    Measured (2,758-comment synthetic stream) this costs ~150 tokens more
    than an 8-per-category floor did, out of ~29k total, so capping them
    bought negligible savings at the cost of real misses. Only "other" is
    sampled to fit whatever budget remains; if the guaranteed categories
    alone exceed `budget`, the total legitimately exceeds it too -- that's
    the tradeoff of "always include", not a bug."""
    selected = [c for c in candidates if c["_category"] in _RARE_CATEGORIES]
    used_ids = {id(c) for c in selected}

    remaining_pool = [c for c in candidates if id(c) not in used_ids]
    spike_pool = [c for c in remaining_pool if c["_in_spike"]]
    normal_pool = [c for c in remaining_pool if not c["_in_spike"]]

    remaining_budget = max(0, budget - len(selected))
    spike_budget = min(len(spike_pool), round(remaining_budget * SPIKE_SHARE))
    for c in _even_sample(spike_pool, spike_budget):
        selected.append(c)
        used_ids.add(id(c))

    normal_budget = max(0, budget - len(selected))
    selected.extend(_even_sample(normal_pool, normal_budget))

    selected.sort(key=lambda c: c["time"])
    return selected


def _summarize_topic_buckets(comments: list[dict]) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for c in comments:
        buckets[_minute_bucket(c["time"], TOPIC_BUCKET_MINUTES)].append(c)

    result = []
    for key in sorted(buckets):
        texts = [(c.get("comment") or "").strip() for c in buckets[key]]
        counts = Counter(t for t in texts if t)
        repeated = [{"text": text, "count": n} for text, n in counts.most_common(3) if n > 1]
        result.append({"bucket": key, "comment_count": len(buckets[key]), "top_repeated": repeated})
    return result
