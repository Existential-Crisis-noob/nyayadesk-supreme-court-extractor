#!/usr/bin/env python3
"""Supreme Court dataset extractor.

Downloads only the selected yearly metadata Parquet files from the public
vanga/indian-supreme-court-judgments S3 bucket, filters them with DuckDB, and
exports rows to CSV, JSONL, or SQLite. PDF text extraction is optional.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import ssl
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import duckdb
except ImportError:  # surfaced as a friendly setup error later
    duckdb = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    import certifi
except ImportError:
    certifi = None


DATASET_BASE = "https://indian-supreme-court-judgments.s3.amazonaws.com"
DATASET_REPOSITORY = "https://github.com/vanga/indian-supreme-court-judgments"
DATASET_LICENSE = "CC-BY-4.0"
USER_AGENT = "NyayaDesk-Supreme-Court-Extractor/1.0"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()

SOURCE_COLUMNS = [
    "title",
    "petitioner",
    "respondent",
    "description",
    "judge",
    "author_judge",
    "citation",
    "case_id",
    "cnr",
    "decision_date",
    "disposal_nature",
    "court",
    "available_languages",
    "raw_html",
    "path",
    "nc_display",
    "scraped_at",
    "year",
]

DERIVED_COLUMNS = [
    "record_id",
    "source_pdf_url",
    "source_metadata_url",
    "source_dataset",
    "source_repository",
    "source_license",
    "extracted_at",
    "source_headnote",
    "judges_normalized",
    "author_judge_normalized",
    "bench_strength",
    "judgment_text",
    "paragraph_count",
    "word_count",
    "text_sha256",
    "text_extraction_method",
]

ANALYSIS_COLUMNS = [
    "ai_headnote",
    "facts",
    "issues",
    "reasoning",
    "holding",
    "ratio",
    "final_order",
    "statutes_cited",
    "cases_cited",
    "citation_treatment",
    "keywords",
    "verification_status",
    "reviewer_status",
    "analysis_status",
    "ai_model",
    "prompt_version",
    "extraction_error",
]

ALL_COLUMNS = SOURCE_COLUMNS + DERIVED_COLUMNS + ANALYSIS_COLUMNS

COLUMN_GROUPS = [
    {
        "id": "identity",
        "label": "Case identity",
        "description": "Names, citations and stable identifiers from the source metadata.",
        "columns": ["record_id", "title", "petitioner", "respondent", "citation", "case_id", "cnr", "nc_display"],
    },
    {
        "id": "court",
        "label": "Court and decision",
        "description": "Decision date, court, disposal and language information.",
        "columns": ["court", "decision_date", "year", "disposal_nature", "available_languages"],
    },
    {
        "id": "judges",
        "label": "Judges and bench",
        "description": "Original and normalized judge fields for analytics.",
        "columns": ["judge", "author_judge", "judges_normalized", "author_judge_normalized", "bench_strength"],
    },
    {
        "id": "source",
        "label": "Source and provenance",
        "description": "Raw source fields, URLs, licence and extraction timestamp.",
        "columns": ["description", "raw_html", "path", "scraped_at", "source_pdf_url", "source_metadata_url", "source_dataset", "source_repository", "source_license", "extracted_at"],
    },
    {
        "id": "text",
        "label": "Judgment text",
        "description": "Optional PDF text and measurable text properties.",
        "columns": ["source_headnote", "judgment_text", "paragraph_count", "word_count", "text_sha256", "text_extraction_method"],
    },
    {
        "id": "analysis",
        "label": "Analysis-ready columns",
        "description": "Intentionally blank until a separate verified enrichment pipeline runs.",
        "columns": ANALYSIS_COLUMNS,
    },
]

DEFAULT_COLUMNS = [
    "record_id",
    "title",
    "petitioner",
    "respondent",
    "citation",
    "case_id",
    "cnr",
    "decision_date",
    "year",
    "disposal_nature",
    "court",
    "judge",
    "author_judge",
    "bench_strength",
    "available_languages",
    "source_headnote",
    "path",
    "source_pdf_url",
    "source_metadata_url",
    "source_license",
    "verification_status",
    "analysis_status",
]


class ExtractionCancelled(Exception):
    pass


class ExtractionError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, Any]], None]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_years(spec: str) -> list[int]:
    """Parse 2023, 2020-2024, or combinations into sorted unique years."""
    years: set[int] = set()
    for token in (part.strip() for part in spec.split(",")):
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            years.update(range(start, end + 1))
        else:
            years.add(int(token))
    current_year = dt.datetime.now().year
    invalid = [year for year in years if year < 1950 or year > current_year]
    if invalid:
        raise ValueError(f"Years must be between 1950 and {current_year}: {invalid}")
    if not years:
        raise ValueError("Choose at least one year.")
    return sorted(years)


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})


def fetch_json(url: str, timeout: int = 45) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout, context=SSL_CONTEXT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ExtractionError(f"Dataset returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise ExtractionError(f"Could not reach the dataset: {exc.reason}") from exc


def metadata_index_url(year: int) -> str:
    return f"{DATASET_BASE}/metadata/tar/year={year}/metadata.index.json"


def parquet_url(year: int) -> str:
    return f"{DATASET_BASE}/metadata/parquet/year={year}/metadata.parquet"


def pdf_url(year: str | int, path: str) -> str:
    file_name = path if path.lower().endswith(".pdf") else f"{path}_EN.pdf"
    return f"{DATASET_BASE}/data/pdf/year={year}/english/{file_name}"


def metadata_json_url(year: str | int, path: str) -> str:
    file_name = path if path.lower().endswith(".json") else f"{path}.json"
    return f"{DATASET_BASE}/metadata/json/year={year}/{file_name}"


def inspect_year(year: int) -> dict[str, Any]:
    data = fetch_json(metadata_index_url(year))
    return {
        "year": year,
        "file_count": int(data.get("file_count") or 0),
        "total_size": int(data.get("total_size") or 0),
        "total_size_human": data.get("total_size_human") or "Unknown",
        "updated_at": data.get("updated_at"),
        "parts": len(data.get("parts") or []),
    }


def download_file(url: str, destination: Path, progress: ProgressCallback | None = None, cancel: threading.Event | None = None) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    try:
        with urllib.request.urlopen(_request(url), timeout=120, context=SSL_CONTEXT) as response, temporary.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            completed = 0
            while True:
                if cancel and cancel.is_set():
                    raise ExtractionCancelled()
                block = response.read(1024 * 512)
                if not block:
                    break
                output.write(block)
                completed += len(block)
                if progress:
                    progress({"downloaded": completed, "download_total": total})
        temporary.replace(destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def ensure_parquet(year: int, cache_dir: Path, progress: ProgressCallback | None = None, cancel: threading.Event | None = None) -> Path:
    target = cache_dir / f"year={year}" / "metadata.parquet"
    if target.exists() and target.stat().st_size > 0:
        if progress:
            progress({"message": f"Using cached metadata for {year}", "year": year})
        return target
    if progress:
        progress({"message": f"Downloading metadata for {year}", "year": year})
    return download_file(parquet_url(year), target, progress, cancel)


def _normalise_people(value: Any) -> str:
    if value is None:
        return ""
    pieces = [piece.strip(" ,;") for piece in re.split(r"\s*(?:,|;|\||\band\b)\s*", str(value), flags=re.I)]
    return " | ".join(dict.fromkeys(piece for piece in pieces if piece))


def _coram_from_html(raw_html: Any) -> str:
    if not raw_html:
        return ""
    match = re.search(r"Coram\s*:\s*(.*?)</strong>", str(raw_html), flags=re.I | re.S)
    if not match:
        return ""
    text = re.sub(r"<[^>]+>", "", match.group(1))
    text = html.unescape(text).replace("*", " ")
    return re.sub(r"\s+", " ", text).strip(" ,;")


def _bench_strength(value: Any, raw_html: Any = None) -> str:
    if raw_html:
        plain = html.unescape(re.sub(r"<[^>]+>", " ", str(raw_html)))
        match = re.search(r"Bench\s*:\s*(\d+)\s*Judges?", re.sub(r"\s+", " ", plain), flags=re.I)
        if match:
            return match.group(1)
    normalised = _normalise_people(value)
    return str(len([piece for piece in normalised.split(" | ") if piece])) if normalised else ""


def _extract_pdf_text(url: str, target: Path, progress: ProgressCallback | None, cancel: threading.Event | None) -> tuple[str, str, str]:
    if PdfReader is None:
        raise ExtractionError("PDF text extraction requires pypdf. Run ./run.sh to install it.")
    download_file(url, target, progress, cancel)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    reader = PdfReader(str(target))
    text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    return text, digest, "pypdf"


def enrich_row(
    raw: dict[str, Any],
    include_pdf_text: bool,
    pdf_cache: Path,
    progress: ProgressCallback | None,
    cancel: threading.Event | None,
) -> dict[str, Any]:
    row = {column: raw.get(column, "") for column in SOURCE_COLUMNS}
    year = str(raw.get("year") or "").strip()
    path = str(raw.get("path") or "").strip()
    coram = _coram_from_html(raw.get("raw_html"))
    row.update(
        {
            "record_id": raw.get("case_id") or raw.get("cnr") or path,
            "source_pdf_url": pdf_url(year, path) if year and path else "",
            "source_metadata_url": metadata_json_url(year, path) if year and path else "",
            "source_dataset": "Indian Supreme Court Judgments",
            "source_repository": DATASET_REPOSITORY,
            "source_license": DATASET_LICENSE,
            "extracted_at": utc_now(),
            "source_headnote": raw.get("description") or "",
            "judges_normalized": _normalise_people(coram or raw.get("judge")),
            "author_judge_normalized": _normalise_people(raw.get("author_judge")),
            "bench_strength": _bench_strength(coram or raw.get("judge"), raw.get("raw_html")),
            "judgment_text": "",
            "paragraph_count": "",
            "word_count": "",
            "text_sha256": "",
            "text_extraction_method": "",
            "ai_headnote": "",
            "facts": "",
            "issues": "",
            "reasoning": "",
            "holding": "",
            "ratio": "",
            "final_order": "",
            "statutes_cited": "",
            "cases_cited": "",
            "citation_treatment": "",
            "keywords": "",
            "verification_status": "source_metadata_extracted",
            "reviewer_status": "not_reviewed",
            "analysis_status": "not_run",
            "ai_model": "",
            "prompt_version": "",
            "extraction_error": "",
        }
    )
    if include_pdf_text and row["source_pdf_url"]:
        try:
            pdf_target = pdf_cache / year / f"{path}_EN.pdf"
            if progress:
                progress({"message": f"Extracting judgment text: {path}", "record": path})
            text, digest, method = _extract_pdf_text(row["source_pdf_url"], pdf_target, progress, cancel)
            row["judgment_text"] = text
            row["paragraph_count"] = str(len([p for p in re.split(r"\n\s*\n", text) if p.strip()]))
            row["word_count"] = str(len(re.findall(r"\b\w+\b", text)))
            row["text_sha256"] = digest
            row["text_extraction_method"] = method
            row["verification_status"] = "source_metadata_and_pdf_extracted"
        except Exception as exc:  # one bad PDF must not destroy a multi-year export
            row["extraction_error"] = str(exc)
            row["verification_status"] = "metadata_only_pdf_error"
    return row


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _build_query(paths: list[Path], filters: dict[str, Any], limit: int | None) -> tuple[str, list[Any]]:
    quoted = ", ".join(_sql_string(str(path.resolve())) for path in paths)
    source = f"read_parquet([{quoted}], union_by_name=true, hive_partitioning=true)"
    connection = duckdb.connect(database=":memory:")
    available = {row[0] for row in connection.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
    connection.close()
    conditions: list[str] = []
    params: list[Any] = []

    query_text = str(filters.get("query") or "").strip()
    if query_text:
        searchable = [column for column in ["title", "petitioner", "respondent", "description", "citation", "judge", "author_judge", "nc_display"] if column in available]
        if searchable:
            conditions.append("(" + " OR ".join(f"CAST(\"{column}\" AS VARCHAR) ILIKE ?" for column in searchable) + ")")
            params.extend([f"%{query_text}%"] * len(searchable))

    for key, column in [("judge", "judge"), ("citation", "citation"), ("disposal", "disposal_nature"), ("language", "available_languages")]:
        value = str(filters.get(key) or "").strip()
        if value and column in available:
            if key == "judge" and "raw_html" in available:
                conditions.append('(CAST("judge" AS VARCHAR) ILIKE ? OR CAST("raw_html" AS VARCHAR) ILIKE ?)')
                params.extend([f"%{value}%", f"%{value}%"])
            else:
                conditions.append(f"CAST(\"{column}\" AS VARCHAR) ILIKE ?")
                params.append(f"%{value}%")

    sql = f"SELECT * FROM {source}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    return sql, params


class RowWriter:
    def __init__(self, path: Path, fmt: str, columns: list[str]):
        self.path = path
        self.format = fmt
        self.columns = columns
        self.handle = None
        self.writer = None
        self.database = None
        path.parent.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "RowWriter":
        if self.format == "csv":
            self.handle = self.path.open("w", newline="", encoding="utf-8-sig")
            self.writer = csv.DictWriter(self.handle, fieldnames=self.columns, extrasaction="ignore")
            self.writer.writeheader()
        elif self.format == "jsonl":
            self.handle = self.path.open("w", encoding="utf-8")
        elif self.format == "sqlite":
            self.database = sqlite3.connect(self.path)
            definitions = ", ".join(f'"{column}" TEXT' for column in self.columns)
            self.database.execute(f"CREATE TABLE judgments ({definitions})")
            self.database.execute('CREATE INDEX IF NOT EXISTS idx_judgments_year ON judgments ("year")')
            self.database.execute('CREATE INDEX IF NOT EXISTS idx_judgments_citation ON judgments ("citation")')
        else:
            raise ValueError(f"Unsupported format: {self.format}")
        return self

    def write(self, row: dict[str, Any]) -> None:
        clean = {column: "" if row.get(column) is None else str(row.get(column, "")) for column in self.columns}
        if self.format == "csv":
            self.writer.writerow(clean)
        elif self.format == "jsonl":
            self.handle.write(json.dumps(clean, ensure_ascii=False) + "\n")
        else:
            placeholders = ", ".join("?" for _ in self.columns)
            names = ", ".join(f'"{column}"' for column in self.columns)
            self.database.execute(f"INSERT INTO judgments ({names}) VALUES ({placeholders})", [clean[column] for column in self.columns])

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.database:
            if exc_type is None:
                self.database.commit()
            self.database.close()
        if self.handle:
            self.handle.close()


def extract(
    years: Iterable[int],
    output: Path,
    fmt: str,
    columns: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int | None = None,
    include_pdf_text: bool = False,
    cache_dir: Path | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    if duckdb is None:
        raise ExtractionError("DuckDB is not installed. Run ./run.sh once, then retry.")
    chosen_columns = columns or DEFAULT_COLUMNS
    unknown = [column for column in chosen_columns if column not in ALL_COLUMNS]
    if unknown:
        raise ValueError(f"Unknown columns: {', '.join(unknown)}")
    if include_pdf_text and "judgment_text" not in chosen_columns:
        chosen_columns = chosen_columns + ["judgment_text", "paragraph_count", "word_count", "text_sha256", "text_extraction_method", "extraction_error"]
    chosen_columns = list(dict.fromkeys(chosen_columns))
    cache = cache_dir or Path(__file__).resolve().parent / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    years = list(years)
    parquet_paths: list[Path] = []
    for index, year in enumerate(years, start=1):
        if cancel and cancel.is_set():
            raise ExtractionCancelled()
        if progress:
            progress({"phase": "download", "message": f"Preparing {year}", "year": year, "year_index": index, "year_total": len(years)})
        parquet_paths.append(ensure_parquet(year, cache / "metadata", progress, cancel))

    sql, params = _build_query(parquet_paths, filters or {}, limit)
    connection = duckdb.connect(database=":memory:")
    cursor = connection.execute(sql, params)
    source_columns = [description[0] for description in cursor.description]
    row_count = 0
    if progress:
        progress({"phase": "extract", "message": "Writing selected rows", "rows": 0})
    try:
        with RowWriter(output, fmt, chosen_columns) as writer:
            while True:
                if cancel and cancel.is_set():
                    raise ExtractionCancelled()
                batch = cursor.fetchmany(200)
                if not batch:
                    break
                for values in batch:
                    raw = dict(zip(source_columns, values))
                    row = enrich_row(raw, include_pdf_text, cache / "pdf", progress, cancel)
                    writer.write(row)
                    row_count += 1
                    if progress and row_count % 25 == 0:
                        progress({"phase": "extract", "message": f"Written {row_count:,} rows", "rows": row_count})
    except Exception:
        if output.exists():
            output.unlink()
        raise
    finally:
        connection.close()
    if progress:
        progress({"phase": "complete", "message": f"Export complete: {row_count:,} rows", "rows": row_count})
    return {"output": str(output), "rows": row_count, "columns": chosen_columns, "years": years, "format": fmt}


def schema_payload() -> dict[str, Any]:
    return {
        "source_columns": SOURCE_COLUMNS,
        "derived_columns": DERIVED_COLUMNS,
        "analysis_columns": ANALYSIS_COLUMNS,
        "all_columns": ALL_COLUMNS,
        "default_columns": DEFAULT_COLUMNS,
        "groups": COLUMN_GROUPS,
        "dataset": {
            "name": "Indian Supreme Court Judgments",
            "repository": DATASET_REPOSITORY,
            "license": DATASET_LICENSE,
            "coverage": "1950 to present, subject to the upstream publication state",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract rows from the Indian Supreme Court judgments public dataset.")
    parser.add_argument("--years", required=True, help="Examples: 2023 or 2020-2024 or 2018,2020,2023")
    parser.add_argument("--output", required=True, type=Path, help="Output .csv, .jsonl, or .sqlite file")
    parser.add_argument("--format", choices=["csv", "jsonl", "sqlite"], default="csv")
    parser.add_argument("--columns", help="Comma-separated columns. Defaults to the recommended research schema.")
    parser.add_argument("--query", default="", help="Search case title, parties, description, citation and judges")
    parser.add_argument("--judge", default="")
    parser.add_argument("--citation", default="")
    parser.add_argument("--disposal", default="")
    parser.add_argument("--language", default="")
    parser.add_argument("--limit", type=int, default=0, help="Maximum rows; 0 means no limit")
    parser.add_argument("--include-pdf-text", action="store_true", help="Download each matching English PDF and extract text")
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).resolve().parent / ".cache")
    parser.add_argument("--list-columns", action="store_true")
    args = parser.parse_args(argv)

    if args.list_columns:
        print("\n".join(ALL_COLUMNS))
        return 0
    columns = [item.strip() for item in args.columns.split(",") if item.strip()] if args.columns else DEFAULT_COLUMNS
    try:
        result = extract(
            years=parse_years(args.years),
            output=args.output,
            fmt=args.format,
            columns=columns,
            filters={"query": args.query, "judge": args.judge, "citation": args.citation, "disposal": args.disposal, "language": args.language},
            limit=args.limit or None,
            include_pdf_text=args.include_pdf_text,
            cache_dir=args.cache_dir,
            progress=lambda event: print(event.get("message", ""), file=sys.stderr),
        )
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, ExtractionError, ExtractionCancelled) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
