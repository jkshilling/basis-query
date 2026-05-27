"""LLM-generated neutral bill summaries via the Anthropic API.

Why this module exists:
The previous approach surfaced one department's "What does this Bill
do?" section verbatim. That excerpt was written for the Governor's
office and often (a) reflected only one agency's narrow lens, (b)
lobbied implicitly for the recommendation that agency made, or (c)
included department-internal jargon ("alignment with Governor's
priorities", "OMB Component Number"). For a veto-decision-support
dashboard we want a single editorially-neutral synthesis that draws
on every department's blue sheet.

Architecture:
- summarize_bill() takes a bill number, its list of blue sheets
  (from blue_sheets.index()), and the BASIS bill_meta. It returns
  a dict {summary, model, generated_at, input_hash}.
- The Anthropic API key is read from ~/Documents/Claude/.anthropic-key
  (local) or /srv/basis-browser/.anthropic-key (production). Same
  pattern as Cloudflare/Stripe/etc.
- Results are cached on disk in .bill_summaries_cache.json keyed by
  a SHA-256 over (billnumber + every blue-sheet description + BASIS
  title). A new blue sheet, an edited description, or a title change
  invalidates the cached summary; everything else is free.
- summarize_bill() is safe to call thousands of times — it only
  contacts the API on a true cache miss.

Fallback chain: When the API is unreachable, the key is missing, or
the call errors, summarize_bill() returns None. Callers should fall
back to bill_summaries.get_bill_summary() (the manual hand-authored
dict — now functions as a per-bill override layer when you want to
lock in your own wording).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("basis_browser.summarizer")

# API key search path. First match wins.
_KEY_PATHS = (
    os.path.expanduser("~/Documents/Claude/.anthropic-key"),
    "/srv/basis-browser/.anthropic-key",
)

# Model + endpoint. Sonnet 4.6 is the current generation as of
# 2026-05-27 (4-7 is Opus-only at this writing).
_MODEL = "claude-sonnet-4-6"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".bill_summaries_cache.json",
)
_CACHE_LOCK = threading.Lock()
_CACHE: dict | None = None


# Bump this when you change the prompt below — it gets folded into
# the cache key so every cached summary regenerates automatically.
_SYSTEM_PROMPT_VERSION = "v2-substance-only"

_SYSTEM_PROMPT = """You are writing a neutral, factual summary of what an Alaska bill \
DOES. The summary appears on a veto-decision-support dashboard alongside other UI \
elements that already convey department positions; your job is solely to describe the \
substantive content of the bill itself.

You will be given the bill's BASIS metadata followed by every department's blue-sheet \
analysis. Use them as factual source material about the bill's provisions. Your \
summary must:

- Describe what the bill does. Which statutes change, which programs are created or \
  modified, what new requirements / rights / obligations are established.
- Lead with one or two sentences capturing the bill's core change in plain English.
- Stay neutral. Do not advocate or editorialize about whether the policy is good or \
  bad.
- Use direct, declarative English. Avoid the passive "may be" / "is intended to" / \
  "would seek to" register common in agency writing.

Hard constraints — these are NOT optional:

- DO NOT name any department or agency (no "DOH", "DCCED", "DFCS", "Department of \
  Health", etc.) anywhere in the summary.
- DO NOT mention what any department, agency, or person recommends, supports, opposes, \
  or thinks about the bill. The dashboard already displays recommendations \
  separately via colored indicators on each chip.
- DO NOT meta-comment about the blue sheets themselves. No phrases like "the blue \
  sheet does not say", "according to the available analysis", "as described in the \
  documents", "no fiscal implications are disclosed", or similar. Either state facts \
  about the bill or leave them out.
- DO NOT describe the bill as "a compromise", "negotiated", "controversial", or any \
  other characterization. Stick to provisions.
- Format / length: 2-3 paragraphs, ~150-250 words. Hard maximum 300 words. No heading. \
  Do not begin with "This bill" — vary openings. Do not invent facts."""


# --------------------------------------------------------------------------
# Cache helpers
# --------------------------------------------------------------------------

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
            log.warning("summarizer.cache_save_failed err=%r", e)


def _read_api_key() -> str:
    for p in _KEY_PATHS:
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    return f.read().strip()
            except OSError:
                continue
    return ""


# --------------------------------------------------------------------------
# Input hashing and prompt assembly
# --------------------------------------------------------------------------

def _input_hash(billnumber: str, blue_sheets: list, bill_meta: dict | None) -> str:
    # Include the system-prompt version so editing the prompt
    # transparently invalidates every cached summary (no manual
    # cache-clear needed).
    parts = [_SYSTEM_PROMPT_VERSION, billnumber.strip()]
    # Sort blue sheets deterministically so reorderings don't bust cache.
    for s in sorted(blue_sheets or [], key=lambda x: (x.get("agency", ""), x.get("filename", ""))):
        parts.append("|".join([
            s.get("agency", ""),
            s.get("recommendation", ""),
            s.get("description", ""),
        ]))
    if bill_meta:
        parts.append(bill_meta.get("latest_version_title") or "")
        parts.append(bill_meta.get("short_title") or "")
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:24]


def _build_user_message(billnumber: str, blue_sheets: list,
                        bill_meta: dict | None) -> str:
    lines = []
    lines.append(f"Bill: {billnumber}")
    if bill_meta:
        if bill_meta.get("latest_version_title"):
            lines.append(f"Legal title: {bill_meta['latest_version_title']}")
        if bill_meta.get("short_title"):
            lines.append(f"Short title: {bill_meta['short_title']}")
    lines.append("")
    if not blue_sheets:
        lines.append("(No departmental blue sheets on file yet.)")
    else:
        for s in blue_sheets:
            agency = s.get("agency") or "(unknown department)"
            rec = s.get("recommendation") or "no recommendation"
            desc = s.get("description") or "(no analytical text extracted from this sheet)"
            lines.append(f"=== {agency} blue sheet — recommends: {rec} ===")
            lines.append(desc.strip())
            lines.append("")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def summarize_bill(billnumber: str, blue_sheets: list,
                   bill_meta: dict | None = None,
                   *, force: bool = False, timeout: float = 60.0) -> dict | None:
    """Return a neutral synthesized summary for one bill. None on
    failure (API down, key missing, etc.).

    Args:
        billnumber:   e.g. "HB 110"
        blue_sheets:  list from blue_sheets.sheets_for(bn)
        bill_meta:    optional dict from fetch_all_bills (latest_version_title etc.)
        force:        bypass the disk cache; re-summarize even if hash matches
        timeout:      socket timeout for the API call
    """
    bn = (billnumber or "").strip()
    if not bn:
        return None

    h = _input_hash(bn, blue_sheets, bill_meta)
    cache = _load_cache()
    if not force and h in cache:
        return cache[h]

    api_key = _read_api_key()
    if not api_key:
        log.warning("summarizer.no_api_key paths=%s", _KEY_PATHS)
        return None

    user_msg = _build_user_message(bn, blue_sheets, bill_meta)
    body = {
        "model": _MODEL,
        "max_tokens": 600,  # ~250 words target + buffer
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _API_URL,
        data=data,
        headers={
            "x-api-key": api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        log.warning("summarizer.http_error bn=%s status=%s body=%r",
                    bn, e.code, body_text)
        return None
    except (urllib.error.URLError, OSError) as e:
        log.warning("summarizer.network_error bn=%s err=%r", bn, e)
        return None
    except Exception as e:
        log.warning("summarizer.unexpected_error bn=%s err=%r", bn, e)
        return None

    content = resp.get("content") or []
    summary_text = ""
    for chunk in content:
        if chunk.get("type") == "text":
            summary_text += chunk.get("text", "")
    summary_text = summary_text.strip()
    if not summary_text:
        log.warning("summarizer.empty_response bn=%s resp=%r", bn, str(resp)[:200])
        return None

    usage = resp.get("usage") or {}
    out = {
        "summary":      summary_text,
        "model":        _MODEL,
        "generated_at": int(time.time()),
        "input_hash":   h,
        "billnumber":   bn,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }
    cache[h] = out
    _save_cache()
    return out


def get_cached(billnumber: str, blue_sheets: list,
               bill_meta: dict | None = None) -> dict | None:
    """Return the cached summary without ever contacting the API.
    Used by hot paths (per-request handlers) that should never block
    on network. Returns None if no summary is cached for this exact
    input hash."""
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _input_hash(bn, blue_sheets, bill_meta)
    return _load_cache().get(h)


# --------------------------------------------------------------------------
# Manual CLI: python -m bill_summarizer <BN>
# --------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m bill_summarizer <BN> [--force]")
        raise SystemExit(2)
    bn = sys.argv[1]
    force = "--force" in sys.argv[2:]

    import blue_sheets
    from fetch import fetch_all_bills

    sheets = blue_sheets.sheets_for(bn)
    chamber = "H" if bn.upper().startswith("H") else "S"
    meta = None
    for b in fetch_all_bills(chamber, "34",
                              queries=["Sponsors", "Subjects", "Versions", "FiscalNotes"]):
        if b.get("billnumber") == bn:
            meta = b
            break

    print(f"Summarizing {bn} ({len(sheets)} blue sheets)...")
    result = summarize_bill(bn, sheets, meta, force=force)
    if result is None:
        print("FAILED")
        raise SystemExit(1)
    print(f"\nGenerated by {result['model']} ({result['input_tokens']}→{result['output_tokens']} tokens):")
    print()
    print(result["summary"])
