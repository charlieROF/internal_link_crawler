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
  --boilerplate-threshold 0.80 \
  --contact-email you@example.com
```

Required flags:

- `--start-url`: starting page for the crawl.
- `--output-dir`: directory where outputs and checkpoints are written.

Optional flags:

- `--max-pages`: maximum pages to crawl. Default: `2000`.
- `--rate-limit`: per-domain requests per second. Default: `1.0`.
- `--wait-buffer`: extra seconds after network idle for late-rendered content. Default: `2.0`.
- `--boilerplate-threshold`: fraction of crawled pages above which repeated internal links are boilerplate. Default: `0.80`.
- `--include-subdomains`: treat subdomains as internal.
- `--ignore-robots`: ignore `robots.txt` and log a warning.
- `--contact-email`: contact email included in the User-Agent.
- `--resume`: resume from `<output-dir>/.checkpoint.json` without prompting.

If a checkpoint exists and `--resume` is not set, the crawler prompts to resume or start fresh. On `Ctrl+C`, it saves a checkpoint and exits.

## Outputs

The output directory contains:

- `pages.csv`: one row per crawled URL.
- `links.csv`: one row per link instance.
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

## Behavior Notes

- JavaScript-rendered HTML is captured through Playwright Chromium.
- The crawler waits for `networkidle`, applies `--wait-buffer`, and caps page wait time at 30 seconds.
- `robots.txt` is honored by default using the crawler User-Agent.
- Non-HTML resources are recorded as page errors and are not parsed.
- HTTP `4xx` and `5xx` pages are recorded and not parsed for links.
- Boilerplate detection is skipped when fewer than 20 pages are crawled; `is_boilerplate` is blank in that case.
- URL fragments and unsupported schemes such as `mailto:`, `tel:`, and `javascript:` are skipped.

## Test Results

Local verification:

- `python crawl.py --help` displayed all documented flags.
- Python syntax compiled with `python -m compileall internal_link_crawler`.
- Local smoke site on `http://127.0.0.1:8765/`: crawled 6 pages, found 12 links, captured a JavaScript-inserted `/js-page/` link, produced all four output files, and had 0 page failures.

Live crawl verification still needs to be run in an environment with dependencies installed and network access:

- Small static marketing site, 5-20 pages.
- WordPress site with lazy-loaded elements, manually comparing rendered HTML to extracted links.
- Larger 200+ page site, including manual interrupt and `--resume`.

Record the final tested sites, page counts, and anomalies here after those live crawls are completed.
