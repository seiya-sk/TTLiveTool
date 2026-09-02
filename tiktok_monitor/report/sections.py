"""The report's outline, shared by prompt.py (what to ask Claude) and
render.py (how to lay out the result). Add/remove a section by editing this
list only — nothing else needs to change.

"summary" isn't listed here: it's pure arithmetic over already-aggregated
data (see data.py), so it's computed directly in render.py rather than
asked of the model.
"""

REPORT_SECTIONS = [
    {
        "key": "viewer_highlights",
        "title": "数値のハイライト",
        "instruction": (
            "1分単位の時系列データ(視聴者数・コメント数・ギフト(ダイヤ)推移)を見て、"
            "視聴者数が大きく伸びた時間帯・落ちた時間帯を1〜3箇所特定してください。"
            "その時間帯のコメント内容やギフトの集中状況を根拠として挙げながら、"
            "考えられる文脈を説明してください。推測であることが分かる書き方にし、断定は避けてください。"
        ),
        "schema": {
            "type": "string",
            "description": "視聴者数の増減とその文脈に関する分析。Markdown箇条書き推奨。",
        },
    },
    {
        "key": "comment_trends",
        "title": "コメント傾向",
        "instruction": (
            "サンプルとして渡すコメント一覧全体の傾向を分析してください。"
            "ポジティブ/ネガティブの比率の印象、よく出た話題・質問・リクエストを挙げてください。"
            "各コメントのcategoryはキーワード・パターンによる簡易的な仕分けです(厳密な判定ではありません)。"
            "特にcategory='negative'のコメントは、実際に何への不満か"
            "(音声・照明・配信内容・配信者の態度など)を本文から具体的に解釈し、傾向として言及してください。"
            "「かわいすぎ」のように誤ってnegativeに分類された無害なコメントは、"
            "実質的な不満ではないと判断できるものとして自然に無視してください。"
        ),
        "schema": {
            "type": "string",
            "description": "コメント全体の傾向分析。Markdown箇条書き推奨。",
        },
    },
    {
        "key": "visual_feedback",
        "title": "映像フィードバック(スクショ分析)",
        "instruction": (
            "添付された配信画面のスクリーンショットについて、画角(顔・上半身の収まり方、余白、傾き)、"
            "服装(色味・清潔感・配信内容とのマッチ度合い)、背景・照明を分析してください。"
            "配信者本人が改善のための客観的フィードバックを求めている、という前提で書いてください。"
            "「良い/悪い」という断定ではなく、観察された事実とその改善余地、という形にしてください。"
            "顔の特定や個人の評価は行わないでください。"
            "スクリーンショットが添付されていない場合は、その旨のみ記載してください。"
        ),
        "schema": {
            "type": "string",
            "description": "画角・服装・照明の観察事実と改善余地。スクショが無い場合はその旨。",
        },
    },
    {
        "key": "next_stream_suggestions",
        "title": "次回配信への提案",
        "instruction": (
            "これまでの分析(数値のハイライト・コメント傾向・映像フィードバック)を踏まえ、"
            "次回配信に向けた具体的な提案を3〜5個、それぞれ根拠とセットで挙げてください。"
            "開始時刻・話題や企画・画角や照明の調整など、種類が偏らないようにしてください。"
        ),
        "schema": {
            "type": "array",
            "items": {"type": "string"},
            "description": "根拠付きの提案のリスト。各要素が1つの提案(根拠を含む1〜2文)。",
        },
    },
]
