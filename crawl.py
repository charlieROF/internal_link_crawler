from __future__ import annotations

import argparse
import asyncio
import sys

from crawler import CrawlConfig, InternalLinkCrawler, load_existing_checkpoint
from output import checkpoint_path, ensure_output_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl one website and export rendered internal link data for SEO analysis."
    )
    parser.add_argument("--start-url", required=True, help="Starting URL for the crawl.")
    parser.add_argument("--output-dir", required=True, help="Directory for CSV, log, summary, and checkpoint files.")
    parser.add_argument("--max-pages", type=int, default=2000, help="Maximum number of pages to crawl. Default: 2000.")
    parser.add_argument(
        "--no-max-pages",
        action="store_true",
        help="Crawl until the internal URL queue is exhausted.",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=1.0,
        help="Per-domain requests per second. Default: 1.0.",
    )
    parser.add_argument(
        "--wait-buffer",
        type=float,
        default=2.0,
        help="Additional seconds to wait after network idle. Default: 2.0.",
    )
    parser.add_argument(
        "--networkidle-timeout",
        type=float,
        default=5.0,
        help="Maximum seconds to wait for network idle after DOM content loads. Default: 5.0.",
    )
    parser.add_argument(
        "--boilerplate-threshold",
        type=float,
        default=0.80,
        help="Sitewide-link threshold as a fraction of crawled pages. Default: 0.80.",
    )
    parser.add_argument(
        "--include-subdomains",
        action="store_true",
        help="Treat subdomains of the starting registered domain as internal.",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Ignore robots.txt disallow rules and log a warning.",
    )
    parser.add_argument(
        "--contact-email",
        help="Contact email included in the crawler User-Agent.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume automatically from .checkpoint.json if it exists.",
    )
    parser.add_argument(
        "--no-body-text",
        action="store_true",
        help="Skip body content extraction. No page_content.csv is produced.",
    )
    parser.add_argument(
        "--ignore-crawl-query-params",
        default="_pos,_sid,_ss,_fid,bvroute,bvstate",
        help=(
            "Comma-separated query parameters to strip before enqueueing URLs for crawling. "
            "Default: _pos,_sid,_ss,_fid,bvroute,bvstate. Use an empty string to disable."
        ),
    )
    parser.add_argument(
        "--keep-shopify-collection-product-urls",
        action="store_true",
        help=(
            "Do not canonicalize Shopify /collections/<collection>/products/<handle> URLs "
            "to /products/<handle> for crawl deduplication."
        ),
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not args.no_max_pages and args.max_pages <= 0:
        raise ValueError("--max-pages must be greater than 0")
    if args.rate_limit < 0:
        raise ValueError("--rate-limit must be 0 or greater")
    if args.wait_buffer < 0:
        raise ValueError("--wait-buffer must be 0 or greater")
    if args.networkidle_timeout < 0:
        raise ValueError("--networkidle-timeout must be 0 or greater")
    if not 0 < args.boilerplate_threshold <= 1:
        raise ValueError("--boilerplate-threshold must be greater than 0 and less than or equal to 1")


async def main_async(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        output_dir = ensure_output_dir(args.output_dir)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    resume_state = None
    checkpoint = checkpoint_path(output_dir)
    if checkpoint.exists():
        if args.resume or _prompt_resume(checkpoint):
            resume_state = load_existing_checkpoint(output_dir)
        else:
            checkpoint.unlink()

    config = CrawlConfig(
        start_url=args.start_url,
        output_dir=output_dir,
        max_pages=None if args.no_max_pages else args.max_pages,
        rate_limit=args.rate_limit,
        wait_buffer_seconds=args.wait_buffer,
        networkidle_timeout_seconds=args.networkidle_timeout,
        boilerplate_threshold=args.boilerplate_threshold,
        include_subdomains=args.include_subdomains,
        ignore_robots=args.ignore_robots,
        contact_email=args.contact_email,
        body_text_enabled=not args.no_body_text,
        ignored_crawl_query_params=_parse_csv_arg(args.ignore_crawl_query_params),
        canonicalize_shopify_product_urls=not args.keep_shopify_collection_product_urls,
    )
    crawler = InternalLinkCrawler(config)
    try:
        await crawler.run(resume_state=resume_state)
    except KeyboardInterrupt:
        print("\nInterrupted. Checkpoint saved; rerun with --resume to continue.")
        return 130
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1
    return 0


def _prompt_resume(checkpoint) -> bool:
    answer = input(f"Checkpoint found at {checkpoint}. Resume? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def _parse_csv_arg(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
