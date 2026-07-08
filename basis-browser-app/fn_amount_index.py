"""On-disk cache of parsed fiscal-note dollar totals.

Each fiscal-note PDF on akleg.gov has a stable URL — once a note is
filed it gets re-revised under a new URL (different page suffix or
date stamp), so we can treat the URL as a content key. We download
each PDF once, parse it through fiscal_note_parser.parse_fn_amounts,
and stash the result on disk so subsequent app starts don't re-pay
the download + PDF-extract cost.

Cache shape: a single JSON file at .fn_amounts_cache.json mapping
url → parsed result (or null on parse failure).

Concurrency: when bulk-resolving FNs for a bill, we download in
parallel with a small thread pool. Individual PDFs are ~120KB so
~5 concurrent fetches saturates the upstream connection nicely.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

from fiscal_note_parser import parse_fn_amounts

log = logging.getLogger("basis_browser.fn_amounts")

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".fn_amounts_cache.json",
)
_CACHE_LOCK = threading.Lock()
_CACHE: dict | None = None


def _load_cache() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            with open(_CACHE_PATH, "r") as f:
                _CACHE = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _CACHE = {}
    return _CACHE


def _save_cache():
    cache = _load_cache()
    with _CACHE_LOCK:
        tmp = _CACHE_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(cache, f, separators=(",", ":"))
            os.replace(tmp, _CACHE_PATH)
        except OSError as e:
            log.warning("fn_amount_cache.save_failed err=%r", e)


def _rewrite_to_proxy(url: str) -> str:
    """Rewrite www.akleg.gov URLs to the configured BASIS_HOST if
    set. Lets the Cloudflare Worker proxy transparently front akleg
    for hosts (like our droplet) whose IP got blocked. No-op when
    BASIS_HOST is the default origin."""
    host = os.environ.get("BASIS_HOST", "https://www.akleg.gov").rstrip("/")
    if host == "https://www.akleg.gov":
        return url
    return url.replace("https://www.akleg.gov", host, 1)


def _download_pdf(url: str, timeout: float = 25.0) -> bytes | None:
    """Fetch one FN PDF. Returns body bytes or None on any failure."""
    fetch_url = _rewrite_to_proxy(url)
    try:
        req = urllib.request.Request(
            fetch_url,
            headers={"User-Agent": "basis-browser/0.1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        log.warning("fn_amount.download_failed url=%s err=%r", url, e)
        return None


def amount_for_url(url: str) -> dict | None:
    """Return the cached parsed result for one FN URL. Downloads and
    parses on cache miss; persists the result so subsequent calls are
    free. Returns None when the FN can't be parsed (image-only PDF,
    network error, etc.) — caller should treat as 'unparseable'."""
    if not url:
        return None
    cache = _load_cache()
    if url in cache:
        return cache[url]

    body = _download_pdf(url)
    parsed = parse_fn_amounts(body) if body else None
    cache[url] = parsed  # may be None — we remember the failure
    _save_cache()
    return parsed


def amounts_for_urls(urls: Iterable[str], max_workers: int = 5) -> dict[str, dict | None]:
    """Bulk variant. Returns {url: parsed_or_None}. Parallel downloads
    for URLs not already cached."""
    cache = _load_cache()
    urls = [u for u in urls if u]
    if not urls:
        return {}
    out = {}
    todo = []
    for u in urls:
        if u in cache:
            out[u] = cache[u]
        else:
            todo.append(u)
    if not todo:
        return out

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_download_pdf, u): u for u in todo}
        results: dict[str, bytes | None] = {}
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                results[u] = fut.result()
            except Exception:
                results[u] = None
    # Parse serially — pypdf is CPU-bound and not thread-friendly.
    for u, body in results.items():
        parsed = parse_fn_amounts(body) if body else None
        cache[u] = parsed
        out[u] = parsed
    _save_cache()
    return out


def aggregate_bill_total(fn_pdfs: list[dict]) -> dict:
    """Given a bill's list of fn_pdfs (from fetch_all_bills with
    FiscalNotes expansion), compute a per-bill total. Each item has
    keys: name, date, preparer, impact, url.

    Returns:
        {
          'total_dollars': float,        # summed across all parsed FNs
          'parsed_fns':    int,          # how many FNs we successfully parsed
          'zero_fns':      int,          # how many were impact='N' (skipped)
          'indet_fns':     int,          # impact='I' (skipped)
          'unparseable':   int,          # had a URL but parse failed
          'fy_min':        int|None,     # earliest fiscal year covered
          'fy_max':        int|None,     # latest fiscal year covered
          'per_fn':        list of {label, dollars} for parsed FNs
        }

    Only impact='Y' / 'P' / unknown FNs are downloaded — 'N' (none/
    zero) and 'I' (indeterminate) are short-circuited."""
    out = {
        "total_dollars": 0.0,
        "parsed_fns":    0,
        "zero_fns":      0,
        "indet_fns":     0,
        "unparseable":   0,
        "fy_min":        None,
        "fy_max":        None,
        "per_fn":        [],
    }
    urls_to_fetch = []
    for fn in fn_pdfs or []:
        impact = (fn.get("impact") or "").upper()
        if impact == "N":
            out["zero_fns"] += 1
            continue
        if impact == "I":
            out["indet_fns"] += 1
            continue
        if fn.get("url"):
            urls_to_fetch.append(fn["url"])

    parsed = amounts_for_urls(urls_to_fetch)
    fy_min = fy_max = None
    for fn in fn_pdfs or []:
        impact = (fn.get("impact") or "").upper()
        if impact in ("N", "I"):
            continue
        url = fn.get("url") or ""
        if not url:
            out["unparseable"] += 1
            continue
        p = parsed.get(url)
        if p is None:
            out["unparseable"] += 1
            continue
        out["parsed_fns"] += 1
        out["total_dollars"] += p["total_dollars"]
        out["per_fn"].append({
            "label":   fn.get("name") or "FN",
            "dollars": p["total_dollars"],
            "fy_start": p["fy_start"],
            "fy_end":   p["fy_end"],
        })
        if fy_min is None or p["fy_start"] < fy_min:
            fy_min = p["fy_start"]
        if fy_max is None or p["fy_end"] > fy_max:
            fy_max = p["fy_end"]
    out["fy_min"] = fy_min
    out["fy_max"] = fy_max
    return out
