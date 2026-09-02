"""USD/JPY exchange rate fetch + storage. The dashboard's "取得" button on
the home page shells out to this module (`python -m tiktok_monitor.fxrate`)
rather than reimplementing the fetch in TypeScript, so there is exactly one
place that talks to the external rate API. Stored in app_settings so both
the dashboard and any future Python-side cost calculation read the same
value regardless of which one last fetched it.
"""
import argparse
import json
import logging
import urllib.request
from urllib.error import URLError

from . import db

logger = logging.getLogger(__name__)

# Frankfurter (ECB-sourced) -- free, no API key, no rate limit hassle for
# this tool's usage pattern (a manual button click, at most a few times a
# day).
FX_API_URL = "https://api.frankfurter.app/latest?from=USD&to=JPY"
SETTING_KEY = "usd_jpy_rate"
DEFAULT_RATE = 150.0


def _parse_rate(payload: dict) -> float:
    return float(payload["rates"]["JPY"])


def fetch_usd_jpy_rate(timeout: float = 10.0) -> float:
    """Raises urllib.error.URLError/TimeoutError/KeyError/ValueError on
    network or parse failure -- callers decide the fallback (e.g. keep
    whatever was previously stored) rather than this silently guessing."""
    # Frankfurter returns 403 for the default "Python-urllib/x.y" user
    # agent (confirmed against the real API); any non-empty override works.
    request = urllib.request.Request(FX_API_URL, headers={"User-Agent": "TTSLiveTool/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return _parse_rate(json.loads(resp.read()))


def update_stored_rate(conn) -> float:
    rate = fetch_usd_jpy_rate()
    db.set_setting(conn, SETTING_KEY, str(rate))
    return rate


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch the current USD/JPY rate and store it in app_settings.")
    parser.add_argument("--db-path", default="data/tts_live_tool.db")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    conn = db.connect(args.db_path)
    db.init_schema(conn)
    try:
        rate = update_stored_rate(conn)
        logger.info("USD/JPY rate updated: %.2f", rate)
    except (URLError, TimeoutError, KeyError, ValueError) as exc:
        logger.error("failed to fetch USD/JPY rate: %s", exc)
        raise SystemExit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
