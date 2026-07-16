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
  --networkidle-timeout 0.0 \
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
- `--wait-buffer`: seconds to wait after `domcontentloaded` for late/lazy-rendered content. This is the primary render gate. Default: `2.0`.
- `--networkidle-timeout`: optional additional seconds to wait for network idle. Default: `0.0` (disabled). Leave at `0` for Shopify and other stacks that hold connections open and never reach network idle; the `domcontentloaded` + `--wait-buffer` gate is used instead.
- `--ignore-crawl-query-params`: comma-separated query parameters to strip before enqueueing URLs for crawling. Default: `_pos,_sid,_ss,_fid,bvroute,bvstate`.
- `--keep-shopify-collection-product-urls`: do not canonicalize Shopify `/collections/<collection>/products/<handle>` URLs to `/products/<handle>` for crawl deduplication.
- `--boilerplate-threshold`: fraction of crawled pages above which repeated internal links are boilerplate. Default: `0.80`.
- `--include-subdomains`: treat subdomains as internal.
- `--ignore-robots`: ignore `robots.txt` and log a warning.
- `--contact-email`: contact email included in the User-Agent.
- `--resume`: resume from `<output-dir>/.checkpoint.json` without prompting.
- `--no-body-text`: skip body content extraction. No `page_content.csv` is produced.
- `--content-blocks`: also extract structured content blocks in document order (headings with level, paragraphs/text, lists, blockquotes, CTAs and links with resolved hrefs, images with alt, form labels/placeholders/buttons). Writes `page_blocks.csv` and `page_blocks.json`. Use for content inventory and rewrite work when you have no CMS access. `page_content.csv` is unaffected.
- `--no-sitemap`: do not fetch XML sitemaps to seed the frontier. By default the crawler reads `Sitemap:` directives from `robots.txt` (falling back to `/sitemap.xml`), expands sitemap index files, and seeds every listed URL — this finds orphan pages that no internal link points to.
- `--semrush-csv`: optional CSV of SEMRush URLs (e.g. a Site Audit "Crawled Pages" export) to include in the coverage reconciliation report.
- `--gsc-csv`: optional CSV of GSC indexed-page URLs to include in the coverage reconciliation report.
- `--seed-urls`: path to a CSV/text file of additional URLs to seed into the frontier (repeatable). Use to force-fetch coverage-gap URLs that are neither internally linked nor in the sitemap (e.g. the `discovery_gap` rows from a prior reconciliation run).

If a checkpoint exists and `--resume` is not set, the crawler prompts to resume or start fresh. On `Ctrl+C`, it saves a checkpoint and exits.

## Outputs

The output directory contains:

- `pages.csv`: one row per crawled URL.
- `links.csv`: one row per link instance.
- `page_content.csv`: one row per crawled HTML URL where body extraction ran.
- `page_blocks.csv` / `page_blocks.json` (with `--content-blocks`): the page's content blocks in document order. CSV is one row per block (`url, order, type, level, text, href, alt`; lists expand to `list_item` rows) for spreadsheet review; JSON keeps lists grouped and includes inline links per text block. Block `type` is structural only (`heading`, `text`, `paragraph`, `list`, `blockquote`, `cta`, `link`, `button`, `image`, `form_label`, `form_placeholder`, `form_button`) — mapping blocks onto semantic slots like "Feature Grid" or "Social proof" is left to the consuming prompt, since those vary by template.
- `coverage_reconciliation.csv`: one row per URL in the union of the crawl, sitemap, and any provided SEMRush/GSC lists, with `in_crawl`/`in_sitemap`/`in_semrush`/`in_gsc` flags and a `classification` (`crawled` or `discovery_gap`). Use this as the go/no-go on completeness.
- `crawl_log.txt`: timestamped `INFO`, `WARNING`, and `ERROR` events.
- `crawl_summary.json`: crawl totals, configuration, and a `coverage_reconciliation` block with per-source coverage counts.

`pages.csv` columns, in order:

```text
url, final_url, status_code, crawl_depth, title, h1, meta_description,
canonical_url, meta_robots, x_robots_tag, indexable, word_count,
redirect_count, redirect_chain,
internal_outlinks_count, external_outlinks_count, internal_inlinks_count,
crawl_duration_ms, crawl_timestamp, error
```

- `canonical_url`: href from `<link rel="canonical">`, resolved to an absolute URL.
- `meta_robots`: raw content of `<meta name="robots">`.
- `x_robots_tag`: raw `X-Robots-Tag` response header.
- `indexable`: `False` when `noindex` (or a standalone `none` directive) is present in either robots source, otherwise `True`.
- `word_count`: word count of the extracted body text (`0` when body extraction is disabled or found no content).
- `redirect_count`: number of redirect hops before the final response (`0` if the URL was served directly).
- `redirect_chain`: the full hop sequence as `<url> (<status>) > ... > <final_url> (<status>)`, empty when there were no redirects.

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

`page_content.csv.url` matches `pages.csv.url` for joining. `body_text` is cleaned main-page content. A safety ceiling of 50,000 characters (at a word boundary) exists but sits well above real page lengths, so normal pages are never truncated.

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
- The render gate is `domcontentloaded` (from navigation) plus `--wait-buffer`, with a hard page cap of 30 seconds. `networkidle` is only awaited when `--networkidle-timeout` is greater than `0`; it is disabled by default because modern stacks (Shopify analytics/chat/pixels) hold connections open and never reach network idle, which otherwise forces every page to the 30-second cap.
- Scheme-less and bare-domain hrefs are normalized before resolution: protocol-relative `//host/path` adopts the page scheme, and bare hosts such as `www.example.com/x` or `example.com/x` are treated as absolute external URLs rather than being joined onto the page path.
- When `--include-subdomains` is not set, only the exact start host (and its `www.` variant) is treated as internal; sibling subdomains such as `answers.example.com` are external and are not queued.
- `links.csv` `source_url` and `target_url` are canonicalized with the same function used for the crawl frontier, so internal link targets reconcile against `pages.csv` (no phantom "uncrawled" collection-scoped product aliases).
- Crawl discovery strips known non-content query parameters before enqueueing URLs. This prevents Shopify search/recommendation params such as `_pos`, `_sid`, `_ss`, `_fid` and Bazaarvoice review params such as `bvroute`, `bvstate` from creating thousands of duplicate product-page fetches. Link instances are still recorded in `links.csv`.
- Shopify collection-context product URLs are canonicalized for crawling, so `/collections/all/products/example` and `/products/example` are fetched as the same page by default. Use `--keep-shopify-collection-product-urls` to disable this.
- XML sitemaps are ingested at startup and seeded into the frontier (disable with `--no-sitemap`). A link crawler only finds what is linked from pages it parses, so sitemap seeding is what surfaces orphaned-but-indexed pages. Sitemap index files are expanded recursively and gzipped sitemaps are supported.
- A coverage reconciliation report is always written. The crawled URL set is diffed against the sitemap (and any `--semrush-csv` / `--gsc-csv` lists), normalized with the same canonicalization used for the crawl frontier so the diff is not full of false mismatches. `in-source-but-not-crawled` is the discovery gap; `crawled-but-not-in-sitemap` is orphans/cruft/non-canonical.
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
