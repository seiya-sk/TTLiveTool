import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from . import db
from .report.generate import generate_report

DEFAULT_DB_PATH = "data/tts_live_tool.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an AI analysis report (live_reports) for a recorded live session."
    )
    parser.add_argument("session_id", type=int, help="live_sessions.id to generate a report for")
    parser.add_argument("--db-path", default=None, help="SQLite DB file path (default: data/tts_live_tool.db)")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY が設定されていません。環境変数として設定するか、"
            "プロジェクトルートに .env ファイルを作成して ANTHROPIC_API_KEY=sk-... を記載してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = args.db_path or os.environ.get("TTS_DB_PATH", DEFAULT_DB_PATH)
    conn = db.connect(db_path)
    db.init_schema(conn)

    try:
        report_id = generate_report(conn, args.session_id)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print(f"レポートを生成しました (live_reports.id={report_id})")


if __name__ == "__main__":
    main()
