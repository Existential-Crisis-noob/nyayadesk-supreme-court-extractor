"""Bounded synchronous extraction used by the Vercel deployment."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from extractor import ALL_COLUMNS, DEFAULT_COLUMNS, extract, parse_years


MAX_CLOUD_ROWS = 1_000
MAX_CLOUD_YEARS = 5
MAX_RESPONSE_BYTES = 4_000_000
SUPPORTED_FORMATS = {"csv": ("csv", "text/csv; charset=utf-8"), "jsonl": ("jsonl", "application/x-ndjson; charset=utf-8")}


def prepare_cloud_config(config: dict[str, Any]) -> dict[str, Any]:
    years = parse_years(str(config.get("years") or ""))
    if len(years) > MAX_CLOUD_YEARS:
        raise ValueError(f"The live tool supports up to {MAX_CLOUD_YEARS} years per export. Use the local tool for larger jobs.")

    fmt = str(config.get("format") or "csv").lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError("The live tool supports CSV and JSONL. Use the local tool for SQLite.")
    if config.get("include_pdf_text"):
        raise ValueError("Full PDF text extraction is available in the local tool. The live tool exports metadata and source links.")

    requested_limit = int(config.get("limit") or 0)
    if requested_limit < 1:
        raise ValueError(f"Enter a row limit from 1 to {MAX_CLOUD_ROWS:,} for the live tool.")
    if requested_limit > MAX_CLOUD_ROWS:
        raise ValueError(f"The live tool supports up to {MAX_CLOUD_ROWS:,} rows per export. Use the local tool for bulk work.")

    requested_columns = config.get("columns") or DEFAULT_COLUMNS
    if not isinstance(requested_columns, list):
        raise ValueError("Columns must be a list.")
    unknown = [str(column) for column in requested_columns if column not in ALL_COLUMNS]
    if unknown:
        raise ValueError(f"Unknown columns: {', '.join(unknown)}")
    columns = list(dict.fromkeys(requested_columns))
    if not columns:
        raise ValueError("Choose at least one output column.")
    if "judgment_text" in columns:
        raise ValueError("Judgment text is only available with local PDF extraction. Choose source_pdf_url for traceable links.")
    if "raw_html" in columns and requested_limit > 100:
        raise ValueError("Exports containing raw_html are capped at 100 rows to stay within Vercel's response limit.")

    return {
        "years": years,
        "format": fmt,
        "limit": requested_limit,
        "columns": columns,
        "filters": {
            "query": str(config.get("query") or ""),
            "judge": str(config.get("judge") or ""),
            "citation": str(config.get("citation") or ""),
            "disposal": str(config.get("disposal") or ""),
            "language": str(config.get("language") or ""),
        },
    }


def run_cloud_extract(config: dict[str, Any], work_root: Path) -> tuple[bytes, str, str, dict[str, Any]]:
    prepared = prepare_cloud_config(config)
    extension, content_type = SUPPORTED_FORMATS[prepared["format"]]
    year_label = str(prepared["years"][0]) if len(prepared["years"]) == 1 else f"{prepared['years'][0]}-{prepared['years'][-1]}"
    filename = f"supreme-court-{year_label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{extension}"
    output = work_root / filename
    result = extract(
        years=prepared["years"],
        output=output,
        fmt=prepared["format"],
        columns=prepared["columns"],
        filters=prepared["filters"],
        limit=prepared["limit"],
        include_pdf_text=False,
        cache_dir=work_root / "cache",
    )
    body = output.read_bytes()
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("This export is too large for a browser download on Vercel. Use fewer rows/columns or run the local tool.")
    return body, filename, content_type, result
