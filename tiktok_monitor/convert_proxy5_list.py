"""One-off converter: proxy5.net's dashboard export (TXT/CSV, confirmed via
their own FAQ/product pages 2026-08-31 to be a plain list of static
host:port pairs -- NOT a rotating gateway) -> proxy_pool_trial.py's expected
--proxies-file format (one http://user:pass@host:port per line).

proxy5's FAQ states username/password auth "works only in combination with
IP address binding" -- i.e. ONE shared username/password pair per plan
(shown on the plan page, above the "IP Binding" field), applied to whichever
of the plan's IPs you connect through, rather than distinct credentials per
IP. That shared pair is passed once via --user/--password here and applied
to every host:port line.

Confirmed against real proxy5 exports (sample/proxy_http_auth.txt) on
2026-08-31 and 2026-09-01: proxy5's actual per-line shape is
"host:port@user:pass" -- distinct host:port pairs, all sharing the one
username:password pair from the plan page, joined with "@" rather than a
third ":". How MANY lines an export has is plan-dependent (the free trial
exported 50, the paid plan 10, and scaling the plan up would give more),
but the shape is identical across plans and nothing in this converter
depends on the count. --user/--password are unnecessary for this shape
(each line is fully self-contained) but stay supported as a fallback for
the other plausible shapes below, in case a different plan/provider export
ever needs converting with this same script:
  - "host:port@user:pass"      (confirmed real proxy5 shape, per-line creds)
  - "host:port"                (shared --user/--password applied)
  - "host:port:user:pass"      (per-line credentials, alternate colon-joined shape)
  - CSV with a header row containing ip/host and port columns
  - a line that's already a full "http://..." URL is passed through as-is

Run:
    python -m tiktok_monitor.convert_proxy5_list \\
        --input ~/Downloads/proxy5_export.txt \\
        --user YOUR_PROXY5_USERNAME \\
        --password YOUR_PROXY5_PASSWORD \\
        --output data/proxy_pool_trial/proxy5_ips.txt
"""
import argparse
import io
import logging
import os

logger = logging.getLogger(__name__)


def _looks_like_port(value: str) -> bool:
    return value.isdigit() and 1 <= int(value) <= 65535


def _parse_line(line: str, default_user: str, default_password: str) -> str | None:
    """Returns a fully-formed http://user:pass@host:port URL, or None if the
    line is blank/a comment/a CSV header and should be skipped."""
    line = line.strip().rstrip(",")
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line  # already a full URL -- pass through unchanged

    if line.count("@") == 1:
        # confirmed real proxy5 shape: "host:port@user:pass"
        hostport, credentials = line.split("@")
        hostport_parts = [p.strip() for p in hostport.split(":") if p.strip()]
        credential_parts = [p.strip() for p in credentials.split(":") if p.strip()]
        if len(hostport_parts) == 2 and len(credential_parts) == 2 and _looks_like_port(hostport_parts[1]):
            host, port = hostport_parts
            user, password = credential_parts
            return f"http://{user}:{password}@{host}:{port}"
        logger.warning("skipping unrecognized 'host:port@user:pass'-shaped line: %r", line)
        return None

    parts = [p.strip() for p in line.replace(",", ":").split(":") if p.strip()]
    if len(parts) == 2:
        host, port = parts
        if not _looks_like_port(port):
            return None  # a CSV header row ("ip,port") lands here -- skip it
        return f"http://{default_user}:{default_password}@{host}:{port}"
    if len(parts) == 4:
        host, port, user, password = parts
        if not _looks_like_port(port):
            return None
        return f"http://{user}:{password}@{host}:{port}"

    logger.warning("skipping unrecognized line (expected host:port or host:port:user:pass): %r", line)
    return None


def convert(raw_text: str, default_user: str, default_password: str) -> list[str]:
    """Order-preserving, deduplicated. Sniffs CSV (comma-separated with a
    header) vs plain host:port-per-line -- _parse_line already tolerates
    commas by normalizing them to ':', so a plain csv.reader pass isn't
    needed; this just iterates lines uniformly either way."""
    urls: list[str] = []
    seen: set[str] = set()
    for line in io.StringIO(raw_text):
        url = _parse_line(line, default_user, default_password)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="Raw TXT/CSV file downloaded from proxy5's dashboard")
    parser.add_argument(
        "--user", default=None, help="Shared proxy5 username (plan page, above 'IP Binding') -- required unless every line already embeds its own host:port:user:pass"
    )
    parser.add_argument("--password", default=None, help="Shared proxy5 password, paired with --user")
    parser.add_argument(
        "--output", default="data/proxy_pool_trial/proxy5_ips.txt", help="Where to write the converted list (default: %(default)s)"
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        raw_text = f.read()

    urls = convert(raw_text, args.user or "", args.password or "")
    if not urls:
        logger.error("no proxy URLs parsed from %s -- check the file's actual format and re-run", args.input)
        raise SystemExit(1)

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for url in urls:
            f.write(url + "\n")

    logger.info("wrote %d proxy URL(s) to %s", len(urls), args.output)


if __name__ == "__main__":
    main()
