"""Builds the actual Claude API request (system + user content) from
aggregated session data. Kept separate from sections.py (what to ask) and
render.py (how to lay out the answer) so the request-building mechanics
can change independently of report structure.
"""
import base64
import json
from pathlib import Path

from .sections import REPORT_SECTIONS

SYSTEM_PROMPT = (
    "あなたはTikTokライブ配信の分析アシスタントです。"
    "配信者が次回配信をより良くするための、具体的で建設的なフィードバックを提供してください。"
    "数値データやコメントサンプルから読み取れる事実に基づいて分析し、"
    "推測は推測であると分かるように書いてください。"
    "出力は日本語で、Markdown文章として自然に読めるようにしてください(箇条書きを適宜使ってください)。"
)

# 配信者本人が運用ノウハウとして貯める「勝ちパターン」ファイル。存在すれば
# 評価基準・提案の引き出しとしてプロンプトに埋め込む。Gitには含めない
# (.gitignoreでcriteria/を除外済み)。存在しない/読めない場合は黙って
# スキップし、従来どおり生成する(グレースフルフォールバック)。
WINNING_PATTERNS_PATH = Path("criteria/winning_patterns.md")


def load_winning_patterns() -> str | None:
    try:
        if not WINNING_PATTERNS_PATH.exists():
            return None
        content = WINNING_PATTERNS_PATH.read_text(encoding="utf-8").strip()
        return content or None
    except OSError:
        return None


def build_system_prompt() -> str:
    winning_patterns = load_winning_patterns()
    if not winning_patterns:
        return SYSTEM_PROMPT
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "以下は配信者自身が過去の経験から蓄積した「勝ちパターン」(評価基準・改善提案の引き出し)です。"
        "分析やご提案の際にこれらの観点を積極的に参考にしてください。\n\n"
        "---\n"
        f"{winning_patterns}\n"
        "---"
    )


def build_user_content(aggregated: dict) -> list[dict]:
    session = aggregated["session"]
    stats = aggregated["basic_stats"]

    instructions = "\n\n".join(f"### {s['title']}\n{s['instruction']}" for s in REPORT_SECTIONS)

    data_text = (
        "# 配信データ\n\n"
        f"配信者: {session['streamer_name']}\n"
        f"配信時間(秒): {stats['duration_seconds']}\n"
        f"視聴者数: 平均 {stats['avg_viewers']} / 最高 {stats['max_viewers']}\n"
        f"総ギフト: {stats['total_diamonds']}ダイヤ({stats['unique_gifters']}人) / コメント数: {stats['comment_count']}件\n\n"
        "## 1分単位の時系列データ(視聴者数 viewers / コメント数 comments / ギフト(ダイヤ) diamonds、"
        "時刻はすべて日本時間JST)\n"
        f"{json.dumps(aggregated['timeseries'], ensure_ascii=False)}\n\n"
        "## ギフトランキング(上位)\n"
        f"{json.dumps(aggregated['gift_ranking'], ensure_ascii=False)}\n\n"
        f"## コメント(代表{len(aggregated['comment_samples'])}件。全コメントをコード側で分析した上で選定: "
        "コメント頻度が急増した時間帯を厚めに、単なる挨拶やスタンプ連打は除外、"
        "質問/リクエスト/ネガティブ反応は種類ごとに代表を含めています。"
        "各要素のcategoryはquestion/request/negative/otherのいずれか。timeは日本時間JST)\n"
        f"{json.dumps(aggregated['comment_samples'], ensure_ascii=False)}\n\n"
        "## コメントの話題傾向(5分バケットごと、全コメントが対象。bucketは日本時間JST。top_repeatedはそのバケット内で"
        "複数人が同じ文言を投稿した場合の頻出テキストで、話題の手がかりとして参考にしてください)\n"
        f"{json.dumps(aggregated['comment_topic_buckets'], ensure_ascii=False)}\n\n"
        f"# 依頼内容\n以下の観点でセクションごとに分析し、指定のツールで提出してください。\n\n{instructions}\n"
    )

    content: list[dict] = [{"type": "text", "text": data_text}]

    screenshot_path = aggregated.get("screenshot_path")
    if screenshot_path and Path(screenshot_path).exists():
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _guess_media_type(screenshot_path),
                    "data": _read_base64(screenshot_path),
                },
            }
        )
    else:
        content.append({"type": "text", "text": "(このセッションには配信画面のスクリーンショットがありません)"})

    return content


def _read_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _guess_media_type(path: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(Path(path).suffix.lower(), "image/png")
