#!/usr/bin/env python3
"""Local web interface for the Supreme Court dataset extractor."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import threading
import uuid
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from extractor import (
    ALL_COLUMNS,
    DEFAULT_COLUMNS,
    ExtractionCancelled,
    ExtractionError,
    extract,
    inspect_year,
    parse_years,
    schema_payload,
)


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
EXPORT_ROOT = ROOT / "exports"
CACHE_ROOT = ROOT / ".cache"
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if key not in {"cancel_event"}}


def set_job(job_id: str, **changes) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(changes)
            JOBS[job_id]["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def run_extraction_job(job_id: str, config: dict) -> None:
    cancel_event = JOBS[job_id]["cancel_event"]

    def progress(event: dict) -> None:
        set_job(job_id, **event)

    try:
        years = parse_years(str(config.get("years") or ""))
        fmt = str(config.get("format") or "csv").lower()
        if fmt not in {"csv", "jsonl", "sqlite"}:
            raise ValueError("Format must be csv, jsonl, or sqlite.")
        requested = config.get("columns") or DEFAULT_COLUMNS
        columns = [column for column in requested if column in ALL_COLUMNS]
        if not columns:
            raise ValueError("Select at least one output column.")
        include_pdf_text = bool(config.get("include_pdf_text"))
        limit = int(config.get("limit") or 0) or None
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        year_label = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
        extension = {"csv": "csv", "jsonl": "jsonl", "sqlite": "sqlite"}[fmt]
        output = EXPORT_ROOT / f"supreme-court-{year_label}-{timestamp}.{extension}"
        set_job(job_id, status="running", phase="download", message="Starting extraction", output_name=output.name)
        result = extract(
            years=years,
            output=output,
            fmt=fmt,
            columns=columns,
            filters={
                "query": config.get("query") or "",
                "judge": config.get("judge") or "",
                "citation": config.get("citation") or "",
                "disposal": config.get("disposal") or "",
                "language": config.get("language") or "",
            },
            limit=limit,
            include_pdf_text=include_pdf_text,
            cache_dir=CACHE_ROOT,
            progress=progress,
            cancel=cancel_event,
        )
        set_job(
            job_id,
            status="complete",
            phase="complete",
            message=f"Exported {result['rows']:,} rows",
            rows=result["rows"],
            columns=result["columns"],
            download_url=f"/download/{output.name}",
            size=output.stat().st_size,
        )
    except ExtractionCancelled:
        set_job(job_id, status="cancelled", phase="cancelled", message="Extraction cancelled")
    except Exception as exc:
        set_job(job_id, status="failed", phase="failed", message=str(exc), error=type(exc).__name__)


class Handler(BaseHTTPRequestHandler):
    server_version = "NyayaDeskExtractor/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload, status=HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, download: bool = False) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(path.stat().st_size))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 512):
                self.wfile.write(block)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/schema":
            self.send_json(schema_payload())
            return
        if path == "/api/inspect":
            try:
                year = int(parse_qs(parsed.query).get("year", [""])[0])
                self.send_json(inspect_year(year))
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = [public_job(job) for job in sorted(JOBS.values(), key=lambda item: item["created_at"], reverse=True)]
            self.send_json({"jobs": jobs})
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]+)", path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match.group(1))
                payload = public_job(job) if job else None
            if payload:
                self.send_json(payload)
            else:
                self.send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
            return
        if path == "/api/files":
            EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
            files = [
                {"name": item.name, "size": item.stat().st_size, "modified": datetime.fromtimestamp(item.stat().st_mtime).isoformat(timespec="seconds"), "download_url": f"/download/{item.name}"}
                for item in sorted(EXPORT_ROOT.iterdir(), key=lambda entry: entry.stat().st_mtime, reverse=True)
                if item.is_file() and item.suffix in {".csv", ".jsonl", ".sqlite"}
            ]
            self.send_json({"files": files})
            return
        if path.startswith("/download/"):
            name = Path(unquote(path.removeprefix("/download/"))).name
            self.send_file(EXPORT_ROOT / name, download=True)
            return
        static_path = "index.html" if path in {"/", ""} else path.lstrip("/")
        candidate = (WEB_ROOT / static_path).resolve()
        try:
            candidate.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self.send_file(candidate)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/extract":
            try:
                config = self.read_json()
                parse_years(str(config.get("years") or ""))
                job_id = uuid.uuid4().hex
                job = {
                    "id": job_id,
                    "status": "queued",
                    "phase": "queued",
                    "message": "Queued",
                    "rows": 0,
                    "config": {key: value for key, value in config.items() if key != "token"},
                    "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "cancel_event": threading.Event(),
                }
                with JOBS_LOCK:
                    JOBS[job_id] = job
                threading.Thread(target=run_extraction_job, args=(job_id, config), daemon=True).start()
                self.send_json(public_job(job), HTTPStatus.ACCEPTED)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        match = re.fullmatch(r"/api/jobs/([a-f0-9]+)/cancel", path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match.group(1))
                if job:
                    job["cancel_event"].set()
            if job:
                self.send_json({"status": "cancelling"})
            else:
                self.send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Supreme Court extractor web interface.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4181)
    parser.add_argument("--open", action="store_true", help="Open the tool in the default browser")
    args = parser.parse_args()
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Supreme Court Extractor running at {url}")
    print("Press Ctrl+C to stop.")
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping extractor.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
