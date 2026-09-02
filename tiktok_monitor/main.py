import argparse
import asyncio
import logging

from . import db
from .client import SessionRunner, run_with_reconnect
from .config import Settings

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor a public TikTok LIVE stream and store comment/gift/viewer_count events."
    )
    parser.add_argument("username", help="TikTok unique_id of the streamer to watch (without @)")
    parser.add_argument("--db-path", default=None, help="SQLite DB file path (default: data/tts_live_tool.db)")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="Seconds with no events before auto-ending the session (default: 60)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging (logs every stored event)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings.from_args(args.username, args.db_path, args.idle_timeout)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    stale_ids = db.recover_stale_live_sessions(conn)
    if stale_ids:
        logger.warning("recovered %d session(s) left 'live' by a previous run: %s", len(stale_ids), stale_ids)
    # Built outside asyncio.run() on purpose: on Windows a KeyboardInterrupt
    # can be delivered while the loop is parked in a blocking selector call
    # and never reach an await point inside run_with_reconnect, so the
    # except/finally below is the only place guaranteed to run manual_end().
    runner = SessionRunner(conn, settings)
    try:
        asyncio.run(run_with_reconnect(runner, settings))
    except KeyboardInterrupt:
        logger.info("keyboard interrupt received, ending session manually")
    finally:
        if not runner.ended:
            runner.manual_end()
        conn.close()


if __name__ == "__main__":
    main()
