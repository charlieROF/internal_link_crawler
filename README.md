# Internal Link Crawler

Python crawler for exporting rendered internal link data for downstream SEO analysis. It crawls one site per run, renders pages in headless Chromium with Playwright, and writes CSV/JSON/log outputs only.

## Setup on macOS Apple Silicon

From a fresh machine:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Python 3.11 or higher is required.

## Usage

Run from this directory:

```bash
python crawl.py \
  --start-url https://example.com/ \
  --output-dir ./crawl-output \
  --max-pages 2000 \
  --rate-limit 1.0 \
  --wait-buffer 2.0 \
  --networkidle-timeout 5.0 \
  --ignore-crawl-query-params _pos,_sid,_ss,_fid,bvroute,bvstate \
  --boilerplate-threshold 0.80 \
  --contact-email you@example.com
```

Required flags:

- `--start-url`: starting page for the crawl.
- `--output-dir`: directory where outputs and checkpoints are written.

Optional flags:

- `--max-pages`: maximum pages to crawl. Default: `2000`.
- `--no-max-pages`: crawl until the internal URL queue is exhausted.
- `--rate-limit`: per-domain requests per second. Default: `1.0`.
- `--wait-buffer`: extra seconds after network idle for late-rendered content. Default: `2.0`.
- `--networkidle-timeout`: maximum seconds to wait for network idle after DOM content loads. Default: `5.0`.
- `--ignore-crawl-query-params`: comma-separated query parameters to strip before enqueueing URLs for crawling. Default: `_pos,_sid,_ss,_fid,bvroute,bvstate`.
- `--keep-shopify-collection-product-urls`: do not canonicalize Shopify `/collections/<collection>/products/<handle>` URLs to `/products/<handle>` for crawl deduplication.
- `--boilerplate-threshold`: fraction of crawled pages above which repeated internal links are boilerplate. Default: `0.80`.
- `--include-subdomains`: treat subdomains as internal.
- `--ignore-robots`: ignore `robots.txt` and log a warning.
- `--contact-email`: contact email included in the User-Agent.
- `--resume`: resume from `<output-dir>/.checkpoint.json` without prompting.
- `--no-body-text`: skip body content extraction. No `page_content.csv` is produced.

If a checkpoint exists and `--resume` is not set, the crawler prompts to resume or start fresh. On `Ctrl+C`, it saves a checkpoint and exits.

## Outputs

The output directory contains:

- `pages.csv`: one row per crawled URL.
- `links.csv`: one row per link instance.
- `page_content.csv`: one row per crawled HTML URL where body extraction ran.
- `crawl_log.txt`: timestamped `INFO`, `WARNING`, and `ERROR` events.
- `crawl_summary.json`: crawl totals and configuration.

`pages.csv` columns, in order:

```text
url, final_url, status_code, crawl_depth, title, h1, meta_description,
internal_outlinks_count, external_outlinks_count, internal_inlinks_count,
crawl_duration_ms, crawl_timestamp, error
```

`links.csv` columns, in order:

```text
source_url, target_url, anchor_text, link_context, is_internal,
is_boilerplate, rel_attribute
```

`page_content.csv` columns, in order:

```text
url, main_heading_text, body_text, extraction_quality, truncated,
original_length, char_count, extracted_at
```

`page_content.csv.url` matches `pages.csv.url` for joining. `body_text` is cleaned main-page content capped at 3,000 characters at a word boundary.

Extraction quality values:

- `main`: extracted from a `<main>` element or `role="main"`.
- `article`: extracted from an `<article>` element.
- `selector`: extracted from a common content container such as `#content`, `.entry-content`, or `.page-content`.
- `fallback`: extracted from the largest visible `div` or `section` when no stronger main-content signal was found.
- `uncertain`: no meaningful body text was found.
- `error`: extraction failed for that page, but the crawl continued.

## Behavior Notes

- JavaScript-rendered HTML is captured through Playwright Chromium.
- Body content is extracted from the same rendered HTML used for link extraction; pages are not fetched a second time.
- Body extraction is enabled by default and can be disabled with `--no-body-text`.
- Body extraction removes common non-content containers before selecting content, including `script`, `style`, `noscript`, `template`, `nav`, `header`, `footer`, `aside`, `form`, cookie/consent widgets, chat widgets, and hidden elements.
- Terminal output includes a live URL progress bar plus fetch/queue/status updates.
- The crawler waits for `networkidle` up to `--networkidle-timeout`, applies `--wait-buffer`, and keeps a hard page cap of 30 seconds.
- Crawl discovery strips known non-content query parameters before enqueueing URLs. This prevents Shopify search/recommendation params such as `_pos`, `_sid`, `_ss`, `_fid` and Bazaarvoice review params such as `bvroute`, `bvstate` from creating thousands of duplicate product-page fetches. Link instances are still recorded in `links.csv`.
- Shopify collection-context product URLs are canonicalized for crawling, so `/collections/all/products/example` and `/products/example` are fetched as the same page by default. Use `--keep-shopify-collection-product-urls` to disable this.
- `robots.txt` is honored by default using the crawler User-Agent.
- Obvious non-page assets such as images, PDFs, scripts, stylesheets, and fonts are recorded as links but are not enqueued for page crawling.
- Other non-HTML resources that are crawled are recorded as page errors and are not parsed.
- HTTP `4xx` and `5xx` pages are recorded and not parsed for links.
- Boilerplate detection is skipped when fewer than 20 pages are crawled; `is_boilerplate` is blank in that case.
- URL fragments and unsupported schemes such as `mailto:`, `tel:`, and `javascript:` are skipped.

`crawl_summary.json` includes a `body_extraction` section with enabled status, extraction count, quality breakdown, truncated count, average character count, and median character count. When `--no-body-text` is set, body extraction is reported as disabled and no `page_content.csv` is written.

## Test Results

Local verification:

- `python crawl.py --help` displayed all documented flags.
- Python syntax compiled with `python -m compileall internal_link_crawler`.
- Local smoke site on `http://127.0.0.1:8765/`: crawled 6 pages, found 12 links, captured a JavaScript-inserted `/js-page/` link, produced all four output files, and had 0 page failures.
- Local v2 smoke site on `http://127.0.0.1:8766/`: crawled 2 pages, produced `page_content.csv`, verified `main` and `article` extraction, and confirmed `--no-body-text` suppresses `page_content.csv`.

Live verification:

- `https://clogdogproducts.com/`: body extraction succeeded with `<main>` found immediately.
- `https://chattanoogacloset.com/`: full crawl with `--no-max-pages` and `--ignore-robots` crawled 96 pages, produced 96 `page_content.csv` rows, had 0 body extraction errors, and reported quality breakdown `main: 96`, `article: 0`, `selector: 0`, `fallback: 0`, `uncertain: 0`, `error: 0`.
