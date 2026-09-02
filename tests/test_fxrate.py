import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.fxrate import _parse_rate


def test_parse_rate_extracts_jpy_value_from_frankfurter_response():
    payload = {"amount": 1, "base": "USD", "date": "2026-08-25", "rates": {"JPY": 149.87}}
    assert _parse_rate(payload) == 149.87
