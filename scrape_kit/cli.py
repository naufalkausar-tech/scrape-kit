from __future__ import annotations

import argparse
import sys
from collections import Counter

import requests

from .downloader import BulkDownloader, read_items
from .export import export_records
from .extract import extract_records
from .polite import DEFAULT_USER_AGENT, PoliteSession, RobotsDisallowed


def parse_fields(pairs: list[str]) -> dict[str, str]:
    fields = {}
    for pair in pairs:
        name, sep, spec = pair.partition("=")
        if not sep or not name.strip():
            raise argparse.ArgumentTypeError(f"--field must look like name=selector[@attr], got {pair!r}")
        fields[name.strip()] = spec.strip()
    return fields


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scrape-kit", description="Polite scraper and resumable bulk downloader")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_http_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--delay", type=float, default=1.0, help="minimum seconds between requests to one host")
        p.add_argument("--retries", type=int, default=3)
        p.add_argument("--timeout", type=float, default=30.0)
        p.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
        p.add_argument("--ignore-robots", action="store_true", help="only with the site owner's permission")

    scrape = sub.add_parser("scrape", help="scrape listing pages into CSV/XLSX")
    scrape.add_argument("start_url")
    scrape.add_argument("--item", required=True, help="CSS selector for one record")
    scrape.add_argument("--field", action="append", default=[], metavar="NAME=SELECTOR[@ATTR]", required=True)
    scrape.add_argument("--next", dest="next_selector", default=None, help="CSS selector of the next-page link")
    scrape.add_argument("--max-pages", type=int, default=None)
    scrape.add_argument("--page-url-column", action="store_true", help="add a _page_url column")
    scrape.add_argument("--out", required=True, help="output .csv or .xlsx")
    add_http_options(scrape)

    download = sub.add_parser("download", help="resumable bulk file download")
    download.add_argument("input", help="text file with one URL per line, or CSV with url[,filename,sha256,size]")
    download.add_argument("--dest", required=True)
    download.add_argument("--manifest", default=None, help="defaults to DEST/manifest.csv")
    add_http_options(download)
    return parser


def make_session(args: argparse.Namespace, session: requests.Session | None) -> PoliteSession:
    return PoliteSession(
        user_agent=args.user_agent,
        min_interval=args.delay,
        max_retries=args.retries,
        timeout=args.timeout,
        respect_robots=not args.ignore_robots,
        session=session,
    )


def run_scrape(args: argparse.Namespace, polite: PoliteSession) -> int:
    fields = parse_fields(args.field)
    max_pages = args.max_pages if args.next_selector else 1
    records = []
    pages = 0
    for response in polite.paginate(args.start_url, args.next_selector or (lambda r: None), max_pages=max_pages):
        pages += 1
        batch = extract_records(response.text, args.item, fields, base_url=response.url)
        if args.page_url_column:
            for record in batch:
                record["_page_url"] = response.url
        records.extend(batch)
        print(f"page {pages}: {len(batch)} records  {response.url}", file=sys.stderr)
    path = export_records(records, args.out)
    print(f"wrote {len(records)} records from {pages} page(s) to {path}")
    return 0


def run_download(args: argparse.Namespace, polite: PoliteSession) -> int:
    items = read_items(args.input)
    downloader = BulkDownloader(args.dest, manifest_path=args.manifest, session=polite)
    results = downloader.run(items)
    counts = Counter(row["status"] for row in results)
    print("  ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "nothing to do")
    print(f"manifest: {downloader.manifest_path}")
    failed = sum(count for status, count in counts.items() if status not in ("downloaded", "skipped"))
    return 1 if failed else 0


def main(argv: list[str] | None = None, session: requests.Session | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    polite = make_session(args, session)
    try:
        if args.command == "scrape":
            return run_scrape(args, polite)
        return run_download(args, polite)
    except RobotsDisallowed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (requests.RequestException, ValueError, argparse.ArgumentTypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
