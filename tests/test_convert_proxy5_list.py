import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.convert_proxy5_list import convert


def test_converts_plain_host_port_lines_with_shared_credentials():
    raw = "1.2.3.4:8080\n5.6.7.8:8080\n"

    assert convert(raw, "shared_user", "shared_pass") == [
        "http://shared_user:shared_pass@1.2.3.4:8080",
        "http://shared_user:shared_pass@5.6.7.8:8080",
    ]


def test_converts_the_confirmed_real_proxy5_host_port_at_user_pass_shape():
    # sample/proxy_http_auth.txt's actual format from the 2026-08-31 free trial.
    raw = (
        "45.148.232.91:8080@mix353MX650N6:1HhCR2R7\n"
        "194.99.26.111:8080@mix353MX650N6:1HhCR2R7\n"
    )

    assert convert(raw, "unused", "unused") == [
        "http://mix353MX650N6:1HhCR2R7@45.148.232.91:8080",
        "http://mix353MX650N6:1HhCR2R7@194.99.26.111:8080",
    ]


def test_converts_host_port_user_pass_lines_using_per_line_credentials():
    raw = "1.2.3.4:8080:u1:p1\n5.6.7.8:8080:u2:p2\n"

    assert convert(raw, "unused", "unused") == [
        "http://u1:p1@1.2.3.4:8080",
        "http://u2:p2@5.6.7.8:8080",
    ]


def test_converts_csv_style_lines_and_skips_a_header_row():
    raw = "ip,port\n1.2.3.4,8080\n5.6.7.8,8080\n"

    assert convert(raw, "u", "p") == [
        "http://u:p@1.2.3.4:8080",
        "http://u:p@5.6.7.8:8080",
    ]


def test_skips_blank_lines_and_comments():
    raw = "1.2.3.4:8080\n\n# a comment\n5.6.7.8:8080\n"

    assert convert(raw, "u", "p") == [
        "http://u:p@1.2.3.4:8080",
        "http://u:p@5.6.7.8:8080",
    ]


def test_dedups_preserving_order():
    raw = "1.2.3.4:8080\n5.6.7.8:8080\n1.2.3.4:8080\n"

    assert convert(raw, "u", "p") == [
        "http://u:p@1.2.3.4:8080",
        "http://u:p@5.6.7.8:8080",
    ]


def test_passes_through_a_line_thats_already_a_full_url():
    raw = "http://someuser:somepass@1.2.3.4:8080\n"

    assert convert(raw, "u", "p") == ["http://someuser:somepass@1.2.3.4:8080"]


def test_skips_and_warns_on_an_unrecognized_line(caplog):
    raw = "1.2.3.4:8080\nnot a proxy line at all\n"

    with caplog.at_level("WARNING"):
        result = convert(raw, "u", "p")

    assert result == ["http://u:p@1.2.3.4:8080"]
    assert any("unrecognized line" in r.message for r in caplog.records)
