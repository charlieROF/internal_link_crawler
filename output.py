from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PAGES_COLUMNS = [
    "url",
    "final_url",
    "status_code",
    "crawl_depth",
    "title",
    "h1",
    "meta_description",
    "canonical_url",
    "meta_robots",
    "x_robots_tag",
    "indexable",
    "word_count",
    "internal_outlinks_count",
    "external_outlinks_count",
    "internal_inlinks_count",
    "crawl_duration_ms",
    "crawl_timestamp",
    "error",
]

LINKS_COLUMNS = [
    "source_url",
    "target_url",
    "anchor_text",
    "link_context",
    "is_internal",
    "is_boilerplate",
    "rel_attribute",
]

RECONCILIATION_COLUMNS = [
    "url",
    "in_crawl",
    "in_sitemap",
    "in_semrush",
    "in_gsc",
    "classification",
]

# Header names commonly used for the URL column in SEMRush / GSC exports.
_URL_COLUMN_HINTS = ("url", "page url", "page", "landing page", "address", "top pages")

PAGE_CONTENT_COLUMNS = [
    "url",
    "main_heading_text",
    "body_text",
    "extraction_quality",
    "truncated",
    "original_length",
    "char_count",
    "extracted_at",
]


class CrawlLogger:
    def __init__(self, output_dir: Path):
        self.path = output_dir / "crawl_log.txt"

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARNING", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def _write(self, level: str, message: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] [{level}] [{message}]\n")


def ensure_output_dir(output_dir: str) -> Path:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_pages_csv(output_dir: Path, pages: list[dict[str, Any]]) -> None:
    with (output_dir / "pages.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PAGES_COLUMNS)
        writer.writeheader()
        for page in pages:
            writer.writerow({column: page.get(column, "") for column in PAGES_COLUMNS})


def write_links_csv(output_dir: Path, links: list[Any]) -> None:
    with (output_dir / "links.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LINKS_COLUMNS)
        writer.writeheader()
        for link in links:
            row = asdict(link) if is_dataclass(link) else dict(link)
            writer.writerow({column: _csv_value(row.get(column)) for column in LINKS_COLUMNS})


def write_page_content_csv(records: list[dict[str, Any]], output_dir: Path) -> None:
    with (output_dir / "page_content.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PAGE_CONTENT_COLUMNS)
        writer.writeheader()
        for record in records:
            writer.writerow({column: _csv_value(record.get(column)) for column in PAGE_CONTENT_COLUMNS})


def write_reconciliation_csv(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    with (output_dir / "coverage_reconciliation.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECONCILIATION_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _csv_value(row.get(column)) for column in RECONCILIATION_COLUMNS})


def read_url_list_csv(path: str) -> list[str]:
    """Extract URLs from an arbitrary CSV export (SEMRush, GSC, etc.).

    Prefers a recognizable URL column; otherwise falls back to any cell that
    looks like an http(s) URL. Tolerant of leading preamble rows.
    """
    urls: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return urls

    url_col = None
    header_idx = None
    for idx, row in enumerate(rows[:15]):
        lowered = [cell.strip().lower() for cell in row]
        for col, value in enumerate(lowered):
            if value in _URL_COLUMN_HINTS:
                url_col, header_idx = col, idx
                break
        if url_col is not None:
            break

    if url_col is not None:
        for row in rows[header_idx + 1:]:
            if url_col < len(row):
                cell = row[url_col].strip()
                if cell.lower().startswith(("http://", "https://")):
                    urls.append(cell)
    else:
        for row in rows:
            for cell in row:
                cell = cell.strip()
                if cell.lower().startswith(("http://", "https://")):
                    urls.append(cell)
    return urls


def write_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    with (output_dir / "crawl_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)


def checkpoint_path(output_dir: Path) -> Path:
    return output_dir / ".checkpoint.json"


def write_checkpoint(output_dir: Path, state: dict[str, Any]) -> None:
    path = checkpoint_path(output_dir)
    serializable = {
        key: [_serialize(item) for item in value] if isinstance(value, list) else value
        for key, value in state.items()
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(serializable, handle)


def load_checkpoint(output_dir: Path) -> dict[str, Any]:
    with checkpoint_path(output_dir).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def delete_checkpoint(output_dir: Path) -> None:
    path = checkpoint_path(output_dir)
    if path.exists():
        path.unlink()


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    return value
