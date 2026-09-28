# scrape-kit

A small, polite web scraper and resumable bulk downloader for Python. It turns listing pages into clean CSV or Excel files without hammering the site.

```bash
scrape-kit scrape https://quotes.toscrape.com/ \
  --item .quote --field "author=.author" --field "author_url=a[href*=author]@href" \
  --field "tags=.tags .keywords@content" --next "li.next a" --max-pages 2 --out authors.xlsx
```

```
page 1: 10 records  https://quotes.toscrape.com/
page 2: 10 records  https://quotes.toscrape.com/page/2/
wrote 20 records from 2 page(s) to authors.xlsx
```

Output: [`examples/authors.csv`](examples/authors.csv) and [`examples/authors.xlsx`](examples/authors.xlsx). The example uses [quotes.toscrape.com](https://quotes.toscrape.com), a site built for scraping practice.

## Why "polite"

- **robots.txt is enforced by default.** Disallowed URLs raise `RobotsDisallowed`, and the CLI exits with code 3. A robots.txt that returns 401/403 is treated as "disallow all". `--ignore-robots` exists only for sites you own or have permission to crawl.
- **Per-host throttling.** A minimum interval between requests to the same host (`--delay`, default 1s). The site's `Crawl-delay` is honoured when it is longer.
- **Retries with backoff.** 429/5xx and connection errors are retried with exponential backoff and jitter. A `Retry-After` header wins when present.
- **An identifiable User-Agent.** Set `--user-agent` to include your contact details.

## Features

- CSS-selector fields: `name=selector` for text, `name=selector@attr` for attributes. Relative `href`/`src` values become absolute URLs.
- Pagination by next-link selector, with a loop guard so the same page is never fetched twice, or via `Link: rel=next` headers or numbered page templates.
- Export to CSV (UTF-8 with BOM, so Excel opens it cleanly) or XLSX (styled header, frozen row, autofilter). Lists are flattened and dicts serialised. Cells that start with `=`, `+`, `-` or `@` can be escaped against formula injection.
- **Bulk downloader** (`scrape-kit download urls.txt --dest files/`): resumes `.part` files with HTTP Range, verifies SHA-256 and size, skips files that are already complete, sanitises filenames, and keeps a CSV manifest of every result (`downloaded / skipped / partial / error / blocked`).

## Install

```bash
pip install -e .
```

Requires Python 3.10+, `requests`, `beautifulsoup4` and `openpyxl`.

## Use it as a library

```python
from scrape_kit import PoliteSession, extract_records, export_records

session = PoliteSession(user_agent="my-bot (me@example.com)", min_interval=2)
records = []
for page in session.paginate("https://quotes.toscrape.com/", "li.next a", max_pages=3):
    records += extract_records(page.text, ".quote", {"author": ".author", "url": "a@href"}, base_url=page.url)
export_records(records, "out.csv")
```

## Tests

```bash
pytest
```

8 offline tests use a fake HTTP session and a fake clock, so nothing touches the network. They cover robots.txt enforcement, crawl-delay throttling, Retry-After handling, field extraction, pagination with a loop guard, the CLI exit code on robots blocks, formula-injection escaping, and a resumed, SHA-256-verified download that is skipped on re-run. CI runs them on every push.

## About

Built by Naufal Kausar ([github.com/naufalkausar-tech](https://github.com/naufalkausar-tech)) with an AI-assisted workflow (Claude Code). I review and test every change before it ships. Related: [cleandata](https://github.com/naufalkausar-tech/cleandata) for cleaning messy CSV/Excel data.

MIT licensed. Please scrape responsibly: respect each site's terms and robots.txt.
