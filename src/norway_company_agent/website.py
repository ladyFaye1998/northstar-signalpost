from __future__ import annotations

import json
import ipaddress
import hashlib
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any

from bs4 import BeautifulSoup
import tldextract
import trafilatura

from .evidence import evidence, utc_now
from .http import archive_bytes

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter", "karriere", "career", "ledig", "jobs", "jobbe",
)
DOMAIN_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())
_ROBOTS_CACHE: dict[str, tuple[float, Any]] = {}
_ROBOTS_LOCK = threading.RLock()
_ORIGIN_LOCKS: dict[str, threading.Lock] = {}
_LAST_FETCH: dict[str, float] = {}
_NETWORK = threading.local()


class CountingHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request: Any) -> Any:
        _NETWORK.requests = getattr(_NETWORK, "requests", 0) + 1
        return super().http_open(request)


class CountingHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request: Any) -> Any:
        _NETWORK.requests = getattr(_NETWORK, "requests", 0) + 1
        return super().https_open(request)


def _read_response(response: Any, limit: int) -> bytes:
    raw = response.read(limit)
    _NETWORK.bytes = getattr(_NETWORK, "bytes", 0) + len(raw)
    archive_bytes(raw)
    return raw


def _origin(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Nonstandard web ports are blocked")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler(), CountingHTTPHandler(), CountingHTTPSHandler())


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = DOMAIN_EXTRACTOR(parsed.hostname or "")
    return ext.top_domain_under_public_suffix or (parsed.hostname or "").lower()


def _robots_allowed(url: str, timeout: float) -> bool:
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    cache_key = _origin(url)
    with _ROBOTS_LOCK:
        cached = _ROBOTS_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 300:
        policy = cached[1]
        return policy if isinstance(policy, bool) else policy.can_fetch(USER_AGENT, url)
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = _read_response(response, 512_001)
            if len(raw) > 512_000 or "html" in response.headers.get("content-type", "").lower():
                raise ValueError("Unreadable robots policy")
            parser.parse(raw.decode("utf-8", errors="replace").splitlines())
        policy: Any = parser
    except urllib.error.HTTPError as exc:
        # Only an explicit missing file permits crawling. Permission, transient, and
        # rate-limit failures are not permission to crawl the website.
        policy = exc.code in {404, 410}
    except Exception:
        policy = False
    with _ROBOTS_LOCK:
        _ROBOTS_CACHE[cache_key] = (time.monotonic(), policy)
    return policy if isinstance(policy, bool) else policy.can_fetch(USER_AGENT, url)


def _pace_request(url: str) -> None:
    origin = _origin(url)
    with _ROBOTS_LOCK:
        policy = (_ROBOTS_CACHE.get(origin) or (0, False))[1]
        lock = _ORIGIN_LOCKS.setdefault(origin, threading.Lock())
    delay = 0.2
    if not isinstance(policy, bool):
        delay = max(delay, float(policy.crawl_delay(USER_AGENT) or 0))
        rate = policy.request_rate(USER_AGENT)
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
    if delay > 3:
        raise ValueError("robots request delay exceeds bounded crawl budget")
    with lock:
        remaining = delay - (time.monotonic() - _LAST_FETCH.get(origin, 0))
        if remaining > 0:
            time.sleep(remaining)
        _LAST_FETCH[origin] = time.monotonic()


class WebsiteRedirectHandler(SafeRedirectHandler):
    def __init__(self, domain: str, timeout: float):
        self.domain, self.timeout = domain, timeout

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        if _registered_domain(newurl) != self.domain:
            raise ValueError("Redirect to a different registered domain needs identity review")
        if not _robots_allowed(newurl, self.timeout):
            raise ValueError("robots policy does not permit redirected URL")
        _pace_request(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _website_open(request: Any, timeout: float, domain: str) -> Any:
    _pace_request(request.full_url)
    return urllib.request.build_opener(WebsiteRedirectHandler(domain, timeout), CountingHTTPHandler(), CountingHTTPSHandler()).open(request, timeout=timeout)


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    groups = (
        ("kontakt", "contact", "locations", "lokasjoner", "avdelinger"),
        ("karriere", "career", "ledig", "jobs", "jobbe", "join-us", "work-with"),
        ("news", "press", "aktuelt", "nyheter", "artikler", "blog"),
        ("om-oss", "om_oss", "about", "ledelse", "management", "team", "people"),
    )
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        if parsed.query or re.search(r"\.(pdf|docx?|xlsx?|zip|jpg|png)$", parsed.path, re.I):
            continue
        rank = next((index for index, terms in enumerate(groups) if any(term in haystack for term in terms)), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates[clean] = min(rank, candidates.get(clean, rank))
    # Cover different information types before spending the budget on several
    # almost identical contact/about pages.
    buckets = [[url for url, group in candidates.items() if group == index] for index in range(len(groups))]
    for bucket in buckets:
        bucket.sort(key=lambda url: (len(urllib.parse.urlparse(url).path), url))
    result = []
    while any(buckets) and len(result) < limit:
        for bucket in buckets:
            if bucket and len(result) < limit:
                result.append(bucket.pop(0))
    return result


def _page_record(raw: bytes, final_url: str) -> tuple[dict[str, Any], BeautifulSoup]:
    archive_bytes(raw)
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    structured: list[Any] = []
    for script in soup.find_all("script", type="application/ld+json")[:30]:
        try:
            body = json.loads(script.string or script.get_text())
            if len(json.dumps(body)) <= 100_000:
                structured.append(body)
        except (ValueError, TypeError, RecursionError):
            continue
    title = soup.title.get_text(" ", strip=True)[:500] if soup.title else ""
    description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
    description = str(description_tag.get("content") or "").strip()[:2000] if description_tag else ""
    text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
    identity_nodes = soup.select("header, footer, [itemprop='legalName'], [itemprop='taxID'], [itemprop='vatID'], address")
    identity_text = " ".join(node.get_text(" ", strip=True) for node in identity_nodes)[:12000]
    links = []
    for anchor in soup.select("a[href]")[:1000]:
        href = str(anchor.get("href") or "").strip()
        absolute = urllib.parse.urljoin(final_url, href)
        if urllib.parse.urlparse(absolute).scheme not in {"http", "https", "mailto", "tel"}:
            continue
        label = anchor.get_text(" ", strip=True) or str(anchor.get("aria-label") or "")
        context = anchor.parent.get_text(" ", strip=True) if anchor.parent else label
        links.append({"url": absolute[:2000], "text": label[:500], "context": context[:1400]})
    dated_links = []
    for time_node in soup.select("time[datetime]")[:60]:
        if str(time_node.get("itemprop") or "").lower() == "datemodified":
            continue
        card = time_node.find_parent("article")
        if card is None:
            # Stop at a small local card; never combine a page-wide timestamp
            # with an unrelated navigation or article title elsewhere.
            ancestor = time_node.parent
            for _ in range(3):
                if ancestor is None or ancestor.name in {"main", "body", "html"}:
                    break
                if ancestor.select("h1 a[href], h2 a[href], h3 a[href], h4 a[href]"):
                    card = ancestor
                    break
                ancestor = ancestor.parent
        if card is None:
            continue
        dates = {str(node.get("datetime") or "") for node in card.select("time[datetime]")}
        headings = card.select("h1 a[href], h2 a[href], h3 a[href], h4 a[href]")
        titles = {(node.get_text(" ", strip=True), urllib.parse.urljoin(final_url, str(node.get("href") or ""))) for node in headings}
        if len(dates) != 1 or len(titles) != 1:
            continue
        title_text, target = next(iter(titles))
        if not title_text or target == final_url:
            continue
        dated_links.append({"title": title_text[:500], "url": target[:2000], "datetime": next(iter(dates)), "context": card.get_text(" ", strip=True)[:1600]})
    return {
        "url": final_url, "title": title, "description": description,
        "main_text_excerpt": text[:12000], "identity_text_excerpt": identity_text,
        "links": links, "structured_data": structured, "dated_links": dated_links[:20],
        "dates": [str(node.get("datetime") or "") for node in soup.select("time[datetime]")[:30]],
        "content_sha256": hashlib.sha256(raw).hexdigest(), "retrieved_at": utc_now(),
    }, soup


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    if not _robots_allowed(url, timeout):
        return None, [], 1, 0, 0, "robots.txt disallows page"
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with _website_open(request, timeout, homepage_domain) as response:
            raw = _read_response(response, max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], 2, len(raw), elapsed, "redirected outside registered domain"
        page, page_soup = _page_record(raw, final_url)
        return page, _social_links(final_url, page_soup), 2, len(raw), elapsed, None
    except Exception as exc:
        return None, [], 2, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def fetch_website(url: str | None, *, timeout: float = 6.0, max_bytes: int = 2_000_000, max_secondary: int = 4, total_budget: float = 30.0) -> tuple[dict[str, Any], dict[str, Any]]:
    start_requests = getattr(_NETWORK, "requests", 0)
    start_bytes = getattr(_NETWORK, "bytes", 0)
    record, metrics = _fetch_website_impl(url, timeout=timeout, max_bytes=max_bytes, max_secondary=max_secondary, total_budget=total_budget)
    metrics["requests"] = getattr(_NETWORK, "requests", 0) - start_requests
    metrics["bytes"] = getattr(_NETWORK, "bytes", 0) - start_bytes
    metrics["request_count_method"] = "urllib_http_handler_attempts_including_robots_and_redirects"
    return record, metrics


def _fetch_website_impl(url: str | None, *, timeout: float, max_bytes: int, max_secondary: int, total_budget: float) -> tuple[dict[str, Any], dict[str, Any]]:
    crawl_started = time.monotonic()
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    if not _robots_allowed(normalized, timeout):
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent or its policy could not be established"), {"requests": 1, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with _website_open(request, timeout, _registered_domain(normalized)) as response:
            content_type = response.headers.get("content-type", "")
            raw = _read_response(response, max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
        homepage, soup = _page_record(raw, final_url)
        text, title, description = homepage["main_text_excerpt"], homepage["title"], homepage["description"]
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text,
            "identity_text_excerpt": homepage["identity_text_excerpt"],
            "social_links": _social_links(final_url, soup),
            "structured_organisations": _jsonld_organisations({"json-ld": homepage["structured_data"]}),
            "content_sha256": homepage["content_sha256"],
            "extraction_state": _extraction_state(text, soup),
        }
        pages = [homepage]
        social = value["social_links"]
        crawl_errors = []
        requests = 2
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = value["registered_domain"]
        for page_url in _priority_links(final_url, soup, limit=max_secondary):
            if time.monotonic() - crawl_started >= total_budget - timeout:
                crawl_errors.append({"url": page_url, "error": "bounded crawl time budget exhausted"})
                break
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=min(max_bytes, 1_000_000),
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                pages.append(page)
                social.extend(page_social)
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())
        value["crawl_errors"] = crawl_errors
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://"):
            first_elapsed = int((time.monotonic() - started) * 1000)
            remaining = total_budget - (time.monotonic() - crawl_started)
            if remaining < timeout:
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note="Website time budget exhausted"), {"requests": 2, "bytes": 0, "latencies_ms": [first_elapsed]}
            record, metrics = fetch_website("http://" + supplied_url, timeout=timeout, max_bytes=max_bytes, max_secondary=max_secondary, total_budget=remaining)
            metrics["requests"] += 2
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
