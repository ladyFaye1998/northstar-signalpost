from __future__ import annotations

import json
import hashlib
import time
import urllib.error
import urllib.request
import threading
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_local = threading.local()
_rate_lock = threading.Lock()
_last_start = 0.0

def start_trace(archive_dir: str | Path | None = None) -> None:
    _local.events = []
    _local.archive_dir = Path(archive_dir) if archive_dir else None

def fetch_trace() -> list[dict]:
    return list(getattr(_local, "events", []))

def archive_bytes(raw: bytes) -> str:
    """Persist immutable source bytes without adding a network telemetry event."""
    digest = hashlib.sha256(raw).hexdigest()
    directory = getattr(_local, "archive_dir", None)
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (digest + ".bin")
        if not target.exists():
            fd, temporary = tempfile.mkstemp(dir=directory, prefix="snapshot-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            os.replace(temporary, target)
    return digest

def _record(url: str, status: int, raw: bytes, elapsed: int, error: str | None = None) -> None:
    digest = hashlib.sha256(raw).hexdigest() if raw else None
    directory = getattr(_local, "archive_dir", None)
    if raw and directory:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (digest + ".bin")
        if not target.exists():
            fd, temporary = tempfile.mkstemp(dir=directory, prefix="snapshot-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            os.replace(temporary, target)
    if hasattr(_local, "events"):
        _local.events.append({"url": url, "status": status, "bytes": len(raw), "elapsed_ms": elapsed,
                              "retrieved_at": _utc_now(), "content_sha256": digest, "error": error})

def _pace() -> None:
    global _last_start
    with _rate_lock:
        delay = 0.125 - (time.monotonic() - _last_start)
        if delay > 0:
            time.sleep(delay)
        _last_start = time.monotonic()


@dataclass
class FetchResult:
    url: str
    status: int
    elapsed_ms: int
    bytes_received: int
    body: Any = None
    error: str | None = None
    content_sha256: str | None = None
    retrieved_at: str | None = None
    effective_at: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_json(url: str, *, timeout: float = 10.0, attempts: int = 2) -> FetchResult:
    last_error = "request failed"
    for attempt in range(attempts):
        _pace()
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "Northstar-Signalpost/1.0 (company research; https://builderr.ai/challenges/signalpost)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(8_000_001)
                elapsed = int((time.monotonic() - started) * 1000)
                _record(url, response.status, raw, elapsed)
                if len(raw) > 8_000_000:
                    return FetchResult(url, 0, elapsed, len(raw), error="response_size_limit", retrieved_at=_utc_now())
                return FetchResult(url, response.status, elapsed, len(raw), json.loads(raw), content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raw = exc.read(8_000_001)
            _record(url, exc.code, raw, elapsed, f"HTTP {exc.code}")
            if exc.code in {404, 410}:
                return FetchResult(url, exc.code, elapsed, len(raw), error=f"HTTP {exc.code}", content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
            last_error = f"HTTP {exc.code}"
            if exc.code in {400, 401, 403, 429}:
                return FetchResult(url, exc.code, elapsed, len(raw), error=last_error, retrieved_at=_utc_now())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
            if not isinstance(exc, json.JSONDecodeError):
                _record(url, 0, b"", int((time.monotonic() - started) * 1000), last_error)
        if attempt + 1 < attempts:
            time.sleep(0.4 * (2**attempt))
    return FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=_utc_now())
