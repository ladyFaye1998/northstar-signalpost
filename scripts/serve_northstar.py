#!/usr/bin/env python3
"""Serve Northstar's local, read-only evaluator UI (Python standard library only).

Usage: python scripts/serve_northstar.py --data output/envelopes.jsonl
Append-only JSONL is reread for every API request. The last valid row for an
organisation wins. No source fetching, refresh jobs, or filesystem writes occur.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
ORG_PATTERN = re.compile(r"[0-9]{9}\Z")


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-JSON numeric constant: {value}")


def read_companies(path: Path, stale_after_days: int = 30) -> dict:
    """Ignore partial/malformed rows; never erase a preceding valid envelope."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Data file exceeds the 256 MiB local viewer limit.")
    latest: dict[str, dict] = {}
    malformed = 0
    valid_rows = 0
    with path.open("rb") as stream:
        while line := stream.readline(MAX_LINE_BYTES + 1):
            if len(line) > MAX_LINE_BYTES:
                # Consume the rest of this oversized row without allocating it.
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_LINE_BYTES + 1)
                malformed += 1
                continue
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8-sig"), parse_constant=_reject_constant)
                org = str(record.get("organisation_number", "")) if isinstance(record, dict) else ""
                if not ORG_PATTERN.fullmatch(org):
                    raise ValueError("Missing or invalid organisation number")
            except (UnicodeError, ValueError, RecursionError):
                malformed += 1
                continue
            latest[org] = record
            valid_rows += 1
    companies = sorted(latest.values(), key=lambda row: str(
        (row.get("profile") or {}).get("name") if isinstance(row.get("profile"), dict)
        else row.get("name") or row["organisation_number"]
    ).casefold())
    return {
        "companies": companies,
        "meta": {
            "records": len(companies),
            "valid_rows": valid_rows,
            "superseded_rows": valid_rows - len(companies),
            "malformed_lines": malformed,
            "loaded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "stale_after_days": stale_after_days,
            "source_file": path.name,
            "selection": "last_valid_row_per_organisation",
        },
    }


def content_policy(html: bytes) -> str:
    # HTML parsing normalizes line endings before CSP hashes are checked.
    # Windows checkouts may contain CRLF even when the authored source uses LF.
    html = html.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    hashes = {}
    for tag in ("script", "style"):
        blocks = re.findall(rb"<" + tag.encode() + rb"[^>]*>(.*?)</" + tag.encode() + rb">", html, re.S)
        hashes[tag] = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(block).digest()).decode() + "'" for block in blocks)
    return (
        "default-src 'none'; "
        f"script-src {hashes['script']}; style-src {hashes['style']}; "
        "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )


class NorthstarHandler(BaseHTTPRequestHandler):
    server_version = "NorthstarLocal/1.0"

    def __init__(self, *args, data_path: Path, ui_path: Path, stale_after_days: int, **kwargs):
        self.data_path = data_path
        self.ui_path = ui_path
        self.stale_after_days = stale_after_days
        super().__init__(*args, **kwargs)

    def _send(self, status: int, body: bytes, media_type: str, *, csp: str | None = None, filename: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, *, filename: str | None = None):
        self._send(status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8", filename=filename)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlsplit(self.path)
        route = unquote(parsed.path)
        detail = re.fullmatch(r"/api/companies/([0-9]{9})/?", route)
        if route in {"/api/companies", "/api/companies/"} or detail:
            try:
                payload = read_companies(self.data_path, self.stale_after_days)
            except FileNotFoundError:
                self._json(503, {"error": "Data file not found. Run the research CLI first, then reload this view."})
                return
            except (OSError, ValueError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "The configured data file could not be read."
                self._json(503, {"error": message})
                return
            if detail:
                org = detail.group(1)
                row = next((item for item in payload["companies"] if str(item["organisation_number"]) == org), None)
                if row is None:
                    self._json(404, {"error": "Organisation is not present in this dataset."})
                else:
                    download = parse_qs(parsed.query).get("download") == ["1"]
                    self._json(200, row, filename=f"northstar-{org}.json" if download else None)
            else:
                self._json(200, payload)
            return
        if route == "/" or re.fullmatch(r"/companies/[0-9]{9}/?", route):
            try:
                html = self.ui_path.read_bytes()
            except OSError:
                self._json(503, {"error": "Viewer file is missing."})
                return
            self._send(200, html, "text/html; charset=utf-8", csp=content_policy(html))
            return
        self._json(404, {"error": "Not found. This viewer exposes only local company records."})

    def do_POST(self):
        self._json(405, {"error": "Read-only viewer. Use the research or refresh CLI to create a new run."})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="JSONL envelopes; the last valid row per organisation is shown")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stale-after-days", type=int, default=30, help="Viewer freshness threshold; does not change stored evidence")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535 or args.stale_after_days < 1:
        parser.error("port must be 0–65535 and stale-after-days must be positive")
    handler = partial(NorthstarHandler, data_path=args.data.resolve(), ui_path=ROOT / "web" / "index.html", stale_after_days=args.stale_after_days)
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        print(f"Northstar local viewer: http://{args.host}:{server.server_port}", flush=True)
        print(f"Reading {args.data.resolve()} — no network research or writes from this server.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
