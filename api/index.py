from __future__ import annotations

import json
import mimetypes
import tempfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cloud_extract import MAX_CLOUD_ROWS, MAX_CLOUD_YEARS, run_cloud_extract
from extractor import inspect_year, parse_years, schema_payload


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"


class handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200, cache_control="no-store"):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, filename: str):
        path = WEB_ROOT / filename
        if not path.is_file():
            self.send_json({"error": "Not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/schema":
            payload = schema_payload()
            payload["cloud"] = {
                "enabled": True,
                "max_rows": MAX_CLOUD_ROWS,
                "max_years": MAX_CLOUD_YEARS,
                "formats": ["csv", "jsonl"],
                "pdf_text": False,
                "persistent_exports": False,
            }
            self.send_json(payload, cache_control="public, max-age=300")
            return
        if parsed.path == "/api/inspect":
            try:
                year_text = parse_qs(parsed.query).get("year", [""])[0]
                year = parse_years(year_text)[0]
                self.send_json(inspect_year(year))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        static_files = {
            "/": "index.html",
            "/web/index.html": "index.html",
            "/styles.css": "styles.css",
            "/web/styles.css": "styles.css",
            "/cloud.css": "cloud.css",
            "/web/cloud.css": "cloud.css",
            "/app.js": "app.js",
            "/web/app.js": "app.js",
        }
        if parsed.path in static_files:
            self.send_static(static_files[parsed.path])
            return
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/extract":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("Request is too large.")
            config = json.loads(self.rfile.read(length) or b"{}")
            with tempfile.TemporaryDirectory(prefix="nyayadesk-") as temporary:
                body, filename, content_type, result = run_cloud_extract(config, Path(temporary))
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("X-NyayaDesk-Rows", str(result["rows"]))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": f"Extraction failed: {exc}"}, 500)
