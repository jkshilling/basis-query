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
import re
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("basis_browser.summarizer")

# API key search path. First match wins. Dedicated BASIS Query workspace key
# (isolated billing) is preferred; the shared key remains a fallback.
_KEY_PATHS = (
    os.path.expanduser("~/Documents/Claude/.anthropic-key-basis-query"),
    "/srv/basis-browser/.anthropic-key-basis-query",
    os.path.expanduser("~/Documents/Claude/.anthropic-key"),
    "/srv/basis-browser/.anthropic-key",
)

# Model tiers — picked per output's stakes:
#
#  - _MODEL (Sonnet 4.6): structural/internal outputs that don't
#    surface as the Governor's-office-facing analysis. Used by
#    stakeholders + impacted_departments. Cheap, plenty good.
#
#  - _HIGH_STAKES_MODEL (Opus 4.8): the analyses staff actually read
#    when advising the Governor — executive_summary + rationale. A
#    bad call here propagates into a veto/sign decision, so quality
#    matters more than cost. ~5× per-call cost vs Sonnet, but volume
#    is bounded (~30 at-Gov bills × 2 calls × ~hourly refresh).
#
#  - _VETO_LETTER_MODEL (Opus 4.8): final-product drafting that goes
#    onto the Governor's desk under his signature.
_MODEL              = "claude-sonnet-4-6"
_HIGH_STAKES_MODEL  = "claude-opus-4-8"
_VETO_LETTER_MODEL  = "claude-opus-4-8"
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
_SYSTEM_PROMPT_VERSION = "v9-exec-22w-max-opus"

_SYSTEM_PROMPT = """You are writing analysis for a veto-decision-support dashboard \
about Alaska bills. For each bill, return a JSON object with TWO fields:

{
  "executive_summary": "<ONE sentence, ≤22 words. This is a HARD LIMIT, not a target. \
Count your words. If you write 23 words you have failed. \
In active voice, saying WHAT THE BILL DOES. Plain English. No filler openers. \
No 'Enacted under X', no 'This bill', no 'Under this legislation'. \
DO NOT start with the bill number ('HB 36 creates...'); the card already \
displays the bill number prominently right next to this summary, so repeating \
it wastes the slot. Start with the substantive change as the subject. \
\
OMNIBUS BILLS: if a bill changes many things, DO NOT enumerate them. \
Pick the SINGLE most consequential change and name only that one. \
The full 'summary' field below has room for the rest. Examples: \
  WRONG (33 words, enumerates): 'Extends the architects, engineers, and land surveyors \
board's sunset date, creates an optional registered interior design credential under \
that board, and clarifies that certified septic installers may work without a \
professional engineer license.' \
  RIGHT (15 words): 'Creates an optional state credential for interior designers under \
Alaska's architects-and-engineers licensing board.' \
\
More GOOD examples: \
  'Creates a treatment foster home license and requires 7-day judicial review of \
foster-child psychiatric hospitalizations.' (17 words) \
  'Establishes gold and silver coin as legal tender; prohibits municipal sales tax on \
specie exchanges.' (16 words) \
  'Joins Alaska to the Occupational Therapy Licensure Compact for cross-state practice.' \
(12 words) \
\
When you have only the legal 'An Act relating to...' title to work from (no blue sheet, \
no briefing packet), still produce a usable summary by paraphrasing the title — never \
return empty.>",

  "summary": "<two paragraphs, 150-250 words total, describing what the bill does \
in more depth. Same rules as the executive_summary plus the paragraph structure \
below. Inside the summary you may reference the bill number naturally.>"
}

INPUT: the bill's BASIS metadata, then any departmental blue-sheet analyses, then \
any briefing-packet body text. Use them as factual source material. Cite specific \
statute numbers (e.g. AS 14.30.360) when they appear.

FORMAT for the "summary" field:

- EXACTLY TWO paragraphs separated by a blank line. No more, no fewer.
- Paragraph 1: 2-3 sentences capturing the bill's core change.
- Paragraph 2: the mechanics — what statutes change, what programs are created or \
  modified, who must do what, key delayed effective dates. 4-6 sentences.
- No heading, no bullets, no inline lists. Flowing prose only.

HARD CONSTRAINTS for BOTH fields — these are NOT optional:

- DO NOT open with bureaucratic filler: NO "Enacted under", "Under this \
  legislation", "Pursuant to", "By means of", "Through changes to", "This bill", \
  "The bill", "This legislation", or "The legislation". Lead with the bill number \
  + a verb that describes the action.
- DO NOT name any executive-branch department or division (no "DOH", "DCCED", \
  "DFCS", "Department of Health", "Department of Fish and Game", etc.). Use \
  neutral descriptors — "the department", "the state agency", or paraphrased \
  language like "the state's fish and game authority". \
  EXCEPTION: when the bill DIRECTLY amends or restructures a specific named body \
  (AIDEA in an AIDEA bill, Permanent Fund Corporation in a PFD bill, the State \
  Board of Education in a curriculum bill, the Alaska Invasive Species Council in \
  a bill creating it), you may name that body. The test: does the bill text \
  itself name this body to establish, amend, or assign duties to it?
- DO NOT mention what any department/agency/person recommends, supports, or \
  opposes. The dashboard displays recommendations elsewhere.
- DO NOT meta-comment about the source materials. No "the blue sheet does not \
  say", "no fiscal implications are disclosed", "is silent on", etc. If a fact \
  isn't in the source, omit it — don't announce its absence.
- DO NOT characterize the bill as "a compromise", "negotiated", "controversial", \
  etc. Stick to provisions.
- Do not invent facts.

Return ONLY the JSON object. No markdown fence. No preamble."""


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
        # Self-heal: pre-fix entries where json.loads failed and the raw
        # JSON text landed in `summary` with `executive_summary` empty.
        # The fix re-parses them in place with strict=False; if parsing
        # still fails the entry is left untouched.
        dirty = False
        for h, entry in list(_CACHE.items()):
            if not isinstance(entry, dict):
                continue
            if entry.get("executive_summary"):
                continue
            s = entry.get("summary", "")
            if not (isinstance(s, str) and s.lstrip().startswith("{")
                    and '"executive_summary"' in s):
                continue
            exec_s = ""
            full_s = ""
            try:
                p = json.loads(s, strict=False)
                exec_s = str(p.get("executive_summary", "")).strip()
                full_s = str(p.get("summary", "")).strip()
            except json.JSONDecodeError:
                # Fallback to the regex extractor for unescaped-inner-quote
                # responses that strict=False still can't parse.
                heur = _heuristic_extract_two_fields(s)
                if heur:
                    exec_s = heur.get("executive_summary", "")
                    full_s = heur.get("summary", "")
            if exec_s and full_s:
                entry["executive_summary"] = exec_s
                entry["summary"] = full_s
                dirty = True
                log.info("summarizer.cache_self_healed bn=%s",
                         entry.get("billnumber", "?"))
        if dirty:
            # Persist the repair so the next process boot doesn't redo it.
            tmp = _CACHE_PATH + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(_CACHE, f, separators=(",", ":"))
                os.replace(tmp, _CACHE_PATH)
            except OSError as e:
                log.warning("summarizer.cache_heal_save_failed err=%r", e)
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


def _forgiving_json_loads(text: str):
    """Parse JSON that may contain raw control characters (notably
    literal '\\n' inside string values, which the LLM emits regularly
    despite "valid JSON only" instructions).

    Tries strict parse first (so well-formed responses stay on the fast
    path), then falls back to strict=False, which RFC-violates by
    allowing control characters in strings — exactly the LLM's typical
    failure mode. Raises json.JSONDecodeError if BOTH attempts fail.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # strict=False permits raw \n, \t, etc. inside string values.
        # This rescues responses where the model put paragraph breaks
        # inside the "summary" value as literal newlines instead of
        # the JSON-required \n escape.
        return json.loads(text, strict=False)


# Regex pattern that extracts {"executive_summary": "...", "summary": "..."}
# from a response even when there are unescaped double quotes inside the
# string values. The LLM emits this exact two-field shape per our prompt,
# so we can lean on its structure rather than fighting raw JSON. Used as a
# last-resort recovery when both strict and strict=False parses fail.
#
# Strategy: lock onto the structural delimiters that are stable (the field
# names and the closing brace), then everything between is the value
# even if it contains stray ".
_TWO_FIELD_RE = re.compile(
    r'^\s*\{\s*'
    r'"executive_summary"\s*:\s*"(?P<exec>.*?)"\s*,\s*'
    r'"summary"\s*:\s*"(?P<summary>.*)"\s*'
    r'\}\s*$',
    re.DOTALL,
)


def _heuristic_extract_two_fields(text: str) -> dict | None:
    """Last-resort recovery when JSON parsing fails on either strict or
    strict=False. Targets the specific output shape our summarizer prompt
    requests: a top-level object with exactly two string fields
    ('executive_summary' and 'summary'). When the LLM emits unescaped
    inner quotes ('The bill defines "specie" narrowly...'), strict JSON
    parsing fails on the second quote — but structurally we can still
    recover both values by anchoring on the field names + outer braces.

    Returns the dict, or None if even the structural shape doesn't match.
    """
    m = _TWO_FIELD_RE.match(text.strip())
    if not m:
        return None
    return {
        "executive_summary": m.group("exec").strip(),
        # Inner quotes survive verbatim — they're presentational, not JSON
        # delimiters. The downstream renderer escapes them for HTML.
        "summary": m.group("summary").strip(),
    }


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

def _input_hash(billnumber: str, blue_sheets: list, bill_meta: dict | None,
                briefing_packets: list = None) -> str:
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
            s.get("action_justification", ""),
            s.get("full_text", ""),
        ]))
    # Briefing-packet body text gets folded into the input hash too,
    # so edits to the briefing-packet content invalidate the cached
    # summary even when blue sheets are unchanged.
    for p in sorted(briefing_packets or [], key=lambda x: x.get("filename", "")):
        parts.append("BP|" + (p.get("body_text") or ""))
    if bill_meta:
        parts.append(bill_meta.get("latest_version_title") or "")
        parts.append(bill_meta.get("short_title") or "")
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:24]


def _build_user_message(billnumber: str, blue_sheets: list,
                        bill_meta: dict | None,
                        briefing_packets: list = None) -> str:
    lines = []
    lines.append(f"Bill: {billnumber}")
    if bill_meta:
        if bill_meta.get("latest_version_title"):
            lines.append(f"Legal title: {bill_meta['latest_version_title']}")
        if bill_meta.get("short_title"):
            lines.append(f"Short title: {bill_meta['short_title']}")
    lines.append("")
    if blue_sheets:
        for s in blue_sheets:
            agency = s.get("agency") or "(unknown department)"
            rec = s.get("recommendation") or "no recommendation"
            desc = (s.get("description") or "").strip()
            just = (s.get("action_justification") or "").strip()
            full = (s.get("full_text") or "").strip()
            lines.append(f"=== {agency} blue sheet — recommends: {rec} ===")
            if desc:
                lines.append("What does this Bill do:")
                lines.append(desc)
            if just:
                lines.append("Action Justification:")
                lines.append(just)
            # If neither section parsed cleanly (OCR'd file, non-
            # standard template like the UA email), fall through to
            # the full first-page text so the LLM still has content.
            if not desc and not just and full:
                lines.append("Full text (template did not parse cleanly):")
                lines.append(full)
            lines.append("")
    else:
        lines.append("(No departmental blue sheets on file yet.)")
        lines.append("")
    # Briefing packets — supplementary substantive content. For bills
    # whose blue sheet is image-only or sparse (UA, JLB, etc.), this
    # may be the only text the LLM has to work with.
    if briefing_packets:
        for p in briefing_packets:
            body = (p.get("body_text") or "").strip()
            if not body:
                continue
            lines.append(f"=== Briefing packet ({p.get('filename','')}) ===")
            lines.append(body)
            lines.append("")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def summarize_bill(billnumber: str, blue_sheets: list,
                   bill_meta: dict | None = None,
                   briefing_packets: list | None = None,
                   *, force: bool = False, timeout: float = 60.0) -> dict | None:
    """Return a neutral synthesized summary for one bill. None on
    failure (API down, key missing, etc.).

    Args:
        billnumber:        e.g. "HB 110"
        blue_sheets:       list from blue_sheets.sheets_for(bn)
        bill_meta:         optional dict from fetch_all_bills
        briefing_packets:  list from briefing_packets.packets_for(bn)
                           — supplies substantive context when blue
                           sheets are sparse or image-only
        force:             bypass disk cache; re-summarize even if hash matches
        timeout:           socket timeout for the API call
    """
    bn = (billnumber or "").strip()
    if not bn:
        return None

    h = _input_hash(bn, blue_sheets, bill_meta, briefing_packets)
    cache = _load_cache()
    if not force and h in cache:
        return cache[h]

    api_key = _read_api_key()
    if not api_key:
        log.warning("summarizer.no_api_key paths=%s", _KEY_PATHS)
        return None

    user_msg = _build_user_message(bn, blue_sheets, bill_meta, briefing_packets)
    body = {
        "model": _HIGH_STAKES_MODEL,
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
    raw_text = ""
    for chunk in content:
        if chunk.get("type") == "text":
            raw_text += chunk.get("text", "")
    raw_text = raw_text.strip()
    if not raw_text:
        log.warning("summarizer.empty_response bn=%s resp=%r", bn, str(resp)[:200])
        return None

    # Strip a markdown fence if the model added one despite instructions.
    if raw_text.startswith("```"):
        raw_text = raw_text.lstrip("`").lstrip()
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].lstrip()
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3].rstrip()

    try:
        parsed = _forgiving_json_loads(raw_text)
        exec_summary = str(parsed.get("executive_summary", "")).strip()
        full_summary = str(parsed.get("summary", "")).strip()
    except json.JSONDecodeError:
        # Last-resort regex recovery before bailing. The LLM occasionally
        # emits unescaped double quotes inside string values
        # ('The bill defines "specie" narrowly...'); strict=False
        # doesn't help, but the two-field structure is still recoverable
        # by anchoring on the field names + braces.
        heur = _heuristic_extract_two_fields(raw_text)
        if heur and heur.get("summary"):
            log.warning("summarizer.json_parse_recovered bn=%s "
                        "(used regex extractor)", bn)
            exec_summary = heur.get("executive_summary", "")
            full_summary = heur.get("summary", "")
        else:
            # True fallback: treat the whole response as the summary,
            # leave exec summary empty so the template extracts from
            # the first sentence.
            log.warning("summarizer.json_parse_failed bn=%s raw=%r",
                        bn, raw_text[:300])
            exec_summary = ""
            full_summary = raw_text

    if not full_summary:
        log.warning("summarizer.empty_summary bn=%s", bn)
        return None

    usage = resp.get("usage") or {}
    out = {
        "executive_summary": exec_summary,
        "summary":           full_summary,
        "model":             resp.get("model") or _HIGH_STAKES_MODEL,
        "generated_at":      int(time.time()),
        "input_hash":        h,
        "billnumber":        bn,
        "input_tokens":      usage.get("input_tokens", 0),
        "output_tokens":     usage.get("output_tokens", 0),
    }
    cache[h] = out
    _save_cache()
    return out


def get_cached(billnumber: str, blue_sheets: list,
               bill_meta: dict | None = None,
               briefing_packets: list | None = None) -> dict | None:
    """Return the cached summary without ever contacting the API.
    Used by hot paths (per-request handlers) that should never block
    on network. Returns None if no summary is cached for this exact
    input hash."""
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _input_hash(bn, blue_sheets, bill_meta, briefing_packets)
    return _load_cache().get(h)


# --------------------------------------------------------------------------
# Recommendation rationale synthesis — separate prompt, separate cache.
# --------------------------------------------------------------------------

_RATIONALE_PROMPT_VERSION = "v2-opus"

_RATIONALE_SYSTEM_PROMPT = """You are helping a veto-decision-support dashboard \
show WHY the various recommendation chips on a bill card landed where they did. \
For one Alaska bill at a time you will receive:

- The bill's BASIS metadata (number, title)
- Each department's blue-sheet Action Justification text (their own stated \
  reasoning for SIGN / VETO / LWOS)
- The GLO heuristic source (one of: 'departments', 'governor-bill', 'veto-proof')
- Any DOL Bill Review text from the briefing packet
- The GLO recommendation + DEPT recommendation rollup

Produce a JSON object with two fields:

{
  "glo_rationale": "<one or two sentences explaining a plausible basis for the \
GLO recommendation given the heuristic source and the departmental analysis>",
  "dept_rationales": [
    {"agency": "<short code>", "text": "<one-sentence concise summary of THIS \
department's reasoning for THEIR pick>"},
    ...
  ]
}

Rules:

- 'glo_rationale' MUST be 1-2 sentences. Do not name specific departments — use \
  neutral phrasing like 'the agencies' or 'departmental reviewers'. The GLO is \
  an internal heuristic, so the rationale should reflect heuristic + departmental \
  consensus reasoning, not 'X department said Y'.
- 'dept_rationales' is one entry per department blue sheet. The 'agency' field \
  is the short code (DOH, DCCED, etc.). The 'text' is a single concise sentence \
  capturing the SUBSTANTIVE concern or benefit driving THIS department's pick — \
  not a restatement of the pick itself.
- If an Action Justification text is empty or 'TBD', set the 'text' to exactly \
  'No justification text provided.'
- Stay factual; do not invent reasoning that isn't in the source text.
- Be tight. The whole JSON object should be < 400 tokens.
- Return ONLY the JSON object — no preamble, no markdown fence."""


def _rationale_input_hash(billnumber, blue_sheets, glo_payload, dept_payload):
    parts = [_RATIONALE_PROMPT_VERSION, (billnumber or "").strip()]
    parts.append((glo_payload or {}).get("source", ""))
    parts.append((glo_payload or {}).get("rec", ""))
    parts.append((dept_payload or {}).get("rec", ""))
    for s in sorted(blue_sheets or [], key=lambda x: (x.get("agency", ""), x.get("filename", ""))):
        parts.append("|".join([
            s.get("agency", ""),
            s.get("recommendation", ""),
            s.get("action_justification", ""),
        ]))
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()
    return "r:" + digest[:22]


def _build_rationale_user_message(billnumber, bill_meta, blue_sheets,
                                   glo_payload, dept_payload):
    lines = []
    lines.append(f"Bill: {billnumber}")
    if bill_meta:
        title = bill_meta.get("latest_version_title") or bill_meta.get("short_title")
        if title:
            lines.append(f"Title: {title}")
    glo_rec = (glo_payload or {}).get("rec") or "(no rec)"
    glo_src = (glo_payload or {}).get("source") or "(no source)"
    dept_rec = (dept_payload or {}).get("rec") or "(no rec)"
    lines.append("")
    lines.append(f"GLO recommendation: {glo_rec}   (heuristic source: {glo_src})")
    lines.append(f"DEPT recommendation rollup: {dept_rec}")
    lines.append("")
    if not blue_sheets:
        lines.append("(No blue sheets on file.)")
    else:
        for s in blue_sheets:
            agency = s.get("agency") or "?"
            rec = s.get("recommendation") or "no rec"
            just = (s.get("action_justification") or "").strip() or "(no justification text in blue sheet)"
            lines.append(f"=== {agency} blue sheet — recommends {rec} ===")
            lines.append("Action Justification:")
            lines.append(just)
            lines.append("")
    return "\n".join(lines).strip()


def synthesize_rationale(billnumber, blue_sheets, bill_meta,
                         glo_payload, dept_payload,
                         *, force=False, timeout=60.0):
    """Generate concise GLO + per-dept rationales. Cached on disk so
    we only pay the API once per (bill + content + heuristic source).
    Returns dict with keys glo_rationale, dept_rationales, model,
    generated_at. None on failure."""
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _rationale_input_hash(bn, blue_sheets, glo_payload, dept_payload)
    cache = _load_cache()
    if not force and h in cache:
        return cache[h]

    api_key = _read_api_key()
    if not api_key:
        log.warning("rationale.no_api_key")
        return None

    user_msg = _build_rationale_user_message(
        bn, bill_meta, blue_sheets, glo_payload, dept_payload,
    )
    body = {
        "model": _HIGH_STAKES_MODEL,
        "max_tokens": 700,
        "system": _RATIONALE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(body).encode("utf-8"),
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
        log.warning("rationale.http_error bn=%s status=%s body=%r",
                    bn, e.code, body_text)
        return None
    except Exception as e:
        log.warning("rationale.error bn=%s err=%r", bn, e)
        return None

    text = ""
    for chunk in resp.get("content") or []:
        if chunk.get("type") == "text":
            text += chunk.get("text", "")
    text = text.strip()
    # Strip a markdown fence if the model added one despite instructions.
    if text.startswith("```"):
        text = text.lstrip("`").lstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    try:
        parsed = _forgiving_json_loads(text)
    except json.JSONDecodeError:
        log.warning("rationale.bad_json bn=%s raw=%r", bn, text[:300])
        return None

    out = {
        "glo_rationale":   str(parsed.get("glo_rationale", "")).strip(),
        "dept_rationales": parsed.get("dept_rationales", []) or [],
        "model":           resp.get("model") or _HIGH_STAKES_MODEL,
        "generated_at":    int(time.time()),
        "input_hash":      h,
    }
    cache[h] = out
    _save_cache()
    return out


def get_cached_rationale(billnumber, blue_sheets, bill_meta,
                          glo_payload, dept_payload):
    """Read-only cache lookup. Returns None if no rationale is
    cached for this exact input fingerprint."""
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _rationale_input_hash(bn, blue_sheets, glo_payload, dept_payload)
    return _load_cache().get(h)


# --------------------------------------------------------------------------
# Stakeholder synthesis — who benefits / who's burdened / who's otherwise
# affected. Separate cache + prompt from the bill summary so prompt
# iteration on one doesn't invalidate the other.
# --------------------------------------------------------------------------

_STAKEHOLDER_PROMPT_VERSION = "v1"

_STAKEHOLDER_SYSTEM_PROMPT = """You analyze Alaska bills for a veto-decision \
dashboard. From the source material (blue sheets, briefing packets, DOL review), \
identify the CONCRETE STAKEHOLDERS this bill affects.

Return JSON with three lists:

{
  "beneficiaries": ["<concrete group that gains a right, benefit, opportunity, \
or service>", ...],
  "burdened":      ["<concrete group that takes on a new obligation, cost, \
restriction, or risk>", ...],
  "affected":      ["<concrete group that is otherwise materially affected — \
regulated, funded, monitored — but not clearly in the benefit or burden bucket>", ...]
}

RULES — these are NOT optional:

- ONLY list groups DIRECTLY NAMED in the source material. If the blue sheets / \
  briefing packets don't identify a specific group, do NOT invent one to fill \
  the slot. Empty lists are fine and expected.
- Be CONCRETE and SPECIFIC. Good examples: \
    'Licensed occupational therapists practicing across state lines', \
    'Set net commercial salmon permit holders in the Cook Inlet ESSN area', \
    'Foster children admitted for short-term psychiatric care', \
    'Municipal animal control authorities handling feral cats', \
    'Commercial trawl vessels in Alaska finfish fisheries'.
- Bad examples (TOO GENERIC — DO NOT USE): 'all Alaskans', 'the public', \
  'stakeholders', 'agencies', 'the state'.
- Each entry is a short noun phrase, 4-15 words. No periods.
- 0-5 entries per category. Quality over quantity.
- Do not name executive-branch departments or agencies as stakeholders — \
  they're reviewers, not stakeholders for this purpose. Exception: when the \
  bill creates a new obligation/burden on a specific named board/commission \
  (Alaska Invasive Species Council, AIDEA, etc.), naming that body is OK.
- Do not editorialize about whether benefits/burdens are good or bad. Just \
  identify the groups.

Return ONLY the JSON object. No markdown fence. No preamble."""


def _stakeholder_input_hash(billnumber, blue_sheets, briefing_packets, bill_meta):
    parts = [_STAKEHOLDER_PROMPT_VERSION, (billnumber or "").strip()]
    for s in sorted(blue_sheets or [], key=lambda x: (x.get("agency", ""), x.get("filename", ""))):
        parts.append("|".join([
            s.get("agency", ""),
            s.get("description", ""),
            s.get("action_justification", ""),
        ]))
    for p in sorted(briefing_packets or [], key=lambda x: x.get("filename", "")):
        parts.append("BP|" + (p.get("body_text") or ""))
    if bill_meta:
        parts.append(bill_meta.get("latest_version_title") or "")
        parts.append(bill_meta.get("short_title") or "")
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()
    return "s:" + digest[:22]


def _build_stakeholder_user_message(billnumber, bill_meta, blue_sheets, briefing_packets):
    lines = [f"Bill: {billnumber}"]
    if bill_meta:
        title = bill_meta.get("latest_version_title") or bill_meta.get("short_title")
        if title:
            lines.append(f"Title: {title}")
    lines.append("")
    if blue_sheets:
        for s in blue_sheets:
            agency = s.get("agency") or "?"
            lines.append(f"=== {agency} blue sheet ===")
            if s.get("description"):
                lines.append("What does this bill do: " + s["description"])
            if s.get("action_justification"):
                lines.append("Action Justification: " + s["action_justification"])
            lines.append("")
    if briefing_packets:
        for p in briefing_packets:
            body = (p.get("body_text") or "").strip()
            if not body:
                continue
            lines.append(f"=== Briefing packet ({p.get('filename','')}) ===")
            lines.append(body)
            lines.append("")
    return "\n".join(lines).strip()


def synthesize_stakeholders(billnumber, blue_sheets, briefing_packets,
                             bill_meta=None, *, force=False, timeout=60.0):
    """Generate the concrete-stakeholder analysis for one bill.
    Returns dict {beneficiaries, burdened, affected, ...} or None."""
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _stakeholder_input_hash(bn, blue_sheets, briefing_packets, bill_meta)
    cache = _load_cache()
    if not force and h in cache:
        return cache[h]

    api_key = _read_api_key()
    if not api_key:
        return None

    user_msg = _build_stakeholder_user_message(
        bn, bill_meta, blue_sheets, briefing_packets,
    )
    body = {
        "model": _MODEL,
        "max_tokens": 700,
        "system": _STAKEHOLDER_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(body).encode("utf-8"),
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
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            body_text = ""
        log.warning("stakeholders.http_error bn=%s status=%s body=%r",
                    bn, e.code, body_text)
        return None
    except Exception as e:
        log.warning("stakeholders.error bn=%s err=%r", bn, e)
        return None

    text = ""
    for chunk in resp.get("content") or []:
        if chunk.get("type") == "text":
            text += chunk.get("text", "")
    text = text.strip()
    if text.startswith("```"):
        text = text.lstrip("`").lstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    try:
        parsed = _forgiving_json_loads(text)
    except json.JSONDecodeError:
        log.warning("stakeholders.bad_json bn=%s raw=%r", bn, text[:300])
        return None

    def _clean_list(v):
        out = []
        for item in (v or []):
            s = str(item).strip().rstrip(".")
            if 3 <= len(s) <= 200:
                out.append(s)
        return out[:5]

    out = {
        "beneficiaries": _clean_list(parsed.get("beneficiaries")),
        "burdened":      _clean_list(parsed.get("burdened")),
        "affected":      _clean_list(parsed.get("affected")),
        "model":         _MODEL,
        "generated_at":  int(time.time()),
        "input_hash":    h,
    }
    cache[h] = out
    _save_cache()
    return out


def get_cached_stakeholders(billnumber, blue_sheets, briefing_packets, bill_meta=None):
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _stakeholder_input_hash(bn, blue_sheets, briefing_packets, bill_meta)
    return _load_cache().get(h)


# --------------------------------------------------------------------------
# Veto-letter draft synthesis. Generates a Dunleavy-format veto message
# ready to be cleaned up by GLO staff and sent out. Format is anchored
# to the verified-from-the-Senate-Journal template:
#
#   Dear President/Speaker <name>:
#   Under the authority vested in me by Article II, Section 15, of the
#   Alaska Constitution, I have vetoed the following bill:
#   <version-designator>
#   "<full legal title>"
#   I have vetoed <version-designator> for the following reasons:
#   <body paragraphs>
#   Sincerely,
#   /s/
#   Mike Dunleavy
#   Governor
#
# We only generate when flag_gov_pref=='VETO' (gated by callers); the
# cache key includes the gov_pref state so flipping the flag to/from
# VETO is what controls (re)generation.
# --------------------------------------------------------------------------

_VETO_LETTER_PROMPT_VERSION = "v3-akleg-verified"

# Hard-coded for the 34th Legislature. President Stevens verified from
# the April 22 2025 Senate Journal. Speaker Edgmon verified from House
# membership rosters of the 34th. If the dashboard ever has to handle a
# leadership change mid-session, replace with a BASIS member-lookup.
_SENATE_PRESIDENT_LASTNAME = "Stevens"
_HOUSE_SPEAKER_LASTNAME    = "Edgmon"
_SENATE_PRESIDENT_FULL     = "Gary Stevens"
_HOUSE_SPEAKER_FULL        = "Bryce Edgmon"
_SENATE_PRESIDENT_ROOM     = "Capitol Building, Room 111"
_HOUSE_SPEAKER_ROOM        = "Capitol Building, Room 208"
_LEGISLATURE_ZIP           = "Juneau, AK 99801-1182"

# Letterhead block — verbatim from the akleg.gov/PDF/34/Vetoes/* PDFs
# (SB 64, HB 78, HB 26 transcribed via Claude vision OCR, 2026-06-17).
# Two-column layout in the PDFs (Juneau on left, Anchorage on right);
# rendered as a flat block for the plain-text Google Doc — staff
# replaces with Word letterhead at finalization, but the draft
# carries it for completeness.
_LETTERHEAD = (
    "STATE CAPITOL                  550 West Seventh Avenue, Suite 1700\n"
    "P.O. Box 110001                Anchorage, AK 99501\n"
    "Juneau, AK 99811-0001          907-269-7450\n"
    "907-465-3500\n"
    "\n"
    "Governor Mike Dunleavy\n"
    "STATE OF ALASKA"
)


def _format_designator_for_letter(name):
    """Normalize a BASIS version name to formal-letter case.

    BASIS stores 'HCS CSSB 24(FIN) am H' (lowercase 'am'). Real
    Dunleavy veto letters use 'HCS CSSB 24(FIN) AM H' — uppercase
    the bare ' am', ' am H', ' am S', and any '(EFD ... H/S)'
    parenthetical that follows.
    """
    if not name:
        return name
    import re as _re
    # Uppercase ' am ' / ' am$' (with optional H/S after)
    out = _re.sub(r"\bam\b", "AM", name)
    # Uppercase parentheticals that contain effective-date floor
    # actions, e.g. '(efd add S)' -> '(EFD ADD S)', '(efd fld H)' ->
    # '(EFD FLD H)'. These appear after AM markers in some bills.
    def _up_parens(m):
        return "(" + m.group(1).upper() + ")"
    out = _re.sub(r"\((efd[^\)]*)\)", _up_parens, out, flags=_re.IGNORECASE)
    return out

_VETO_LETTER_SYSTEM_PROMPT = """You draft veto letters in the voice of \
Alaska Governor Mike Dunleavy. Output ONLY a JSON object with one field:

{
  "body_paragraphs": ["<paragraph 1>", "<paragraph 2>", ...]
}

You do NOT write the salutation, constitutional citation, bill identification, \
or signature block — those are added programmatically. You write ONLY the 2-4 \
body paragraphs that explain WHY the Governor is vetoing this bill.

VOICE — match Dunleavy's documented rhetorical patterns:
- Open by acknowledging shared ground or the bill's stated goal. Dunleavy's \
opening sentences are almost always non-adversarial: "We agree that...", \
"While the goal of this bill is laudable...", "The administration shares the \
concern that motivates this legislation, however...".
- Sentences are SHORT and concrete. Avoid legal hedging, qualifiers, and \
academic phrasing. Punch sentences ("The amount put forward in this bill \
does not.") are part of the voice.
- The actor is a PRINCIPLE, not a person. Dunleavy says "the fiscal reality \
dictates" or "the administration cannot support" — not "I strongly oppose."
- Close with a constructive offer to keep working with the legislature, OR \
with a clear principle being asserted. The veto letter never slams the door.

CONTENT — the body must be substantively grounded:
- If the override note in the input carries the Governor's specific political \
reasoning, that reasoning IS the spine. Paraphrase it into Dunleavy's voice.
- If departmental blue sheets recommended SIGN/LWOS but the Governor is \
vetoing anyway, address that gap explicitly: cite the broader principle that \
overrides the operational view.
- If departments recommended VETO, lean on their reasoning — paraphrase the \
strongest objection from the blue sheets.
- Cite specific statute numbers or program names when they appear in the source \
material. Avoid generic "this bill is bad" framing.
- DO NOT enumerate every objection. Pick the ONE or TWO strongest reasons. \
A Dunleavy veto letter is 2-4 paragraphs, total length 200-400 words.

STRUCTURE:
- Paragraph 1: acknowledge the bill's goal or shared ground (1-2 sentences)
- Paragraph 2: state the disagreement and the principle behind it (2-3 sentences)
- Paragraph 3 (optional): elaborate or cite the specific concern (2-3 sentences)
- Paragraph 4 (optional, for non-veto-proof bills only): offer to work toward \
a revised bill the administration can support (1-2 sentences)

DO NOT write the closing sentence "For these reasons, I have vetoed this bill." \
That sentence is appended programmatically as its own standalone paragraph after \
your body — every Dunleavy veto-transmittal letter on file places it on its own \
line between the body and "Sincerely,". If you write it, it will be duplicated.

DO NOT write the salutation, "Under the authority..." preamble, bill \
identification, or signature block. Just body_paragraphs. Return ONLY the JSON \
object. No markdown fence. No preamble."""


def _veto_letter_input_hash(billnumber, blue_sheets, briefing_packets,
                            bill_meta, glo_payload, dept_payload, gov_pref):
    parts = [_VETO_LETTER_PROMPT_VERSION, billnumber.strip(), gov_pref or ""]
    for s in sorted(blue_sheets or [],
                    key=lambda x: (x.get("agency", ""), x.get("filename", ""))):
        parts.append("|".join([
            s.get("agency", ""),
            s.get("recommendation", ""),
            s.get("action_justification", ""),
            s.get("description", ""),
        ]))
    for p in sorted(briefing_packets or [], key=lambda x: x.get("filename", "")):
        parts.append("BP|" + (p.get("body_text") or ""))
    if bill_meta:
        parts.append(bill_meta.get("latest_version_title") or "")
        parts.append(bill_meta.get("latest_version_letter") or "")
    parts.append(json.dumps(glo_payload or {}, sort_keys=True))
    parts.append(json.dumps(dept_payload or {}, sort_keys=True))
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()
    return "vl:" + digest[:24]


def _build_veto_letter_user_message(billnumber, blue_sheets, briefing_packets,
                                    bill_meta, glo_payload, dept_payload):
    lines = []
    lines.append(f"Bill: {billnumber}")
    if bill_meta:
        if bill_meta.get("latest_version_title"):
            lines.append(f"Legal title: {bill_meta['latest_version_title']}")
    glo_rec = (glo_payload or {}).get("rec", "")
    glo_note = (glo_payload or {}).get("note", "")
    dept_rec = (dept_payload or {}).get("rec", "")
    dept_note = (dept_payload or {}).get("note", "")
    lines.append("")
    lines.append(f"GLO recommendation: {glo_rec}")
    if glo_note:
        lines.append(f"GLO override / heuristic note (the Governor's "
                     f"distilled political reasoning — this is the SPINE "
                     f"of the veto letter): {glo_note}")
    lines.append("")
    lines.append(f"Departmental rollup: {dept_rec}")
    if dept_note:
        lines.append(f"Dept rollup note: {dept_note}")
    lines.append("")
    if blue_sheets:
        lines.append("=== Departmental blue sheets ===")
        for s in blue_sheets:
            agency = s.get("agency") or "(unknown)"
            rec = s.get("recommendation") or "no rec"
            lines.append(f"--- {agency} (recommends: {rec}) ---")
            desc = (s.get("description") or "").strip()
            just = (s.get("action_justification") or "").strip()
            if desc:
                lines.append("What the bill does (from this dept):")
                lines.append(desc)
            if just:
                lines.append("Dept's stated reasoning:")
                lines.append(just)
            lines.append("")
    if briefing_packets:
        lines.append("=== Briefing packet body ===")
        for p in briefing_packets:
            body = (p.get("body_text") or "").strip()
            if body:
                lines.append(body)
                lines.append("")
    return "\n".join(lines).strip()


def synthesize_veto_letter(billnumber, blue_sheets, bill_meta,
                           glo_payload, dept_payload,
                           briefing_packets=None, gov_pref="VETO",
                           *, force=False, timeout=60.0):
    """Generate the body paragraphs of a Dunleavy-format veto letter
    for one bill. Returns a dict with the structured letter ready to
    render, or None on failure.

    Caches by content-addressed hash that includes gov_pref state so
    flipping the tag back and forth doesn't waste API calls. The
    salutation and constitutional preamble are constants assembled
    at render time, not stored in the cache.
    """
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _veto_letter_input_hash(bn, blue_sheets, briefing_packets,
                                bill_meta, glo_payload, dept_payload,
                                gov_pref)
    cache = _load_cache()
    if not force and h in cache:
        return cache[h]

    api_key = _read_api_key()
    if not api_key:
        log.warning("veto_letter.no_api_key bn=%s", bn)
        return None

    user_msg = _build_veto_letter_user_message(
        bn, blue_sheets, briefing_packets, bill_meta, glo_payload, dept_payload,
    )
    body = {
        "model": _VETO_LETTER_MODEL,
        "max_tokens": 900,
        "system": _VETO_LETTER_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(body).encode("utf-8"),
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
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            body_text = ""
        log.warning("veto_letter.http_error bn=%s status=%s body=%r",
                    bn, e.code, body_text)
        return None
    except Exception as e:
        log.warning("veto_letter.error bn=%s err=%r", bn, e)
        return None

    text = ""
    for chunk in resp.get("content") or []:
        if chunk.get("type") == "text":
            text += chunk.get("text", "")
    text = text.strip()
    if text.startswith("```"):
        text = text.lstrip("`").lstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    try:
        parsed = _forgiving_json_loads(text)
    except json.JSONDecodeError:
        log.warning("veto_letter.bad_json bn=%s raw=%r", bn, text[:300])
        return None

    paragraphs = [str(p).strip() for p in (parsed.get("body_paragraphs") or [])
                  if str(p).strip()]
    if not paragraphs:
        log.warning("veto_letter.empty bn=%s", bn)
        return None

    # Pick salutation + addressee based on bill chamber. HB = Speaker
    # (House origin), SB = President (Senate origin). For concurrent-
    # resolution / joint-resolution chambers we follow the same rule.
    prefix = bn.split()[0] if bn else ""
    if prefix.startswith("H"):
        salutation = f"Dear Speaker {_HOUSE_SPEAKER_LASTNAME}:"
        addressee_chamber = "House"
        recipient_block = (
            f"The Honorable {_HOUSE_SPEAKER_FULL}\n"
            f"Speaker of the House\n"
            f"Alaska State Legislature\n"
            f"{_HOUSE_SPEAKER_ROOM}\n"
            f"{_LEGISLATURE_ZIP}"
        )
    else:
        salutation = f"Dear President {_SENATE_PRESIDENT_LASTNAME}:"
        addressee_chamber = "Senate"
        recipient_block = (
            f"The Honorable {_SENATE_PRESIDENT_FULL}\n"
            f"Senate President\n"
            f"Alaska State Legislature\n"
            f"{_SENATE_PRESIDENT_ROOM}\n"
            f"{_LEGISLATURE_ZIP}"
        )

    # Bill version designator — e.g. "HCS CSSB 24(FIN) AM H" —
    # sourced from BASIS Versions data via the operative (latest
    # non-Z) version's `name` attribute. The Z version is the bare
    # "Enrolled SB X" form which is NOT what the Governor cites.
    # Normalize to uppercase 'AM' / '(EFD ...)' per the verbatim
    # akleg.gov veto letters on file.
    version_designator = _format_designator_for_letter(
        (bill_meta or {}).get("operative_version_name") or bn)
    # Pre-Z title arrives quoted with "An Act" prefix already in it —
    # use verbatim. Fall back to the Z (cleaned) title only as last
    # resort; it lacks the "An Act" prefix and surrounding quotes.
    legal_title = ((bill_meta or {}).get("operative_version_title")
                   or (bill_meta or {}).get("latest_version_title")
                   or "")

    out = {
        # Letterhead block — verbatim from akleg.gov vetoes. Two-column
        # in the PDFs; flat block here.
        "letterhead":         _LETTERHEAD,
        # Placeholder — staff fills in the actual veto signing date.
        "date_line":          "[DATE]",
        "recipient_block":    recipient_block,
        "salutation":         salutation,
        "addressee_chamber":  addressee_chamber,
        # No comma after "Section 15" — matches verbatim Dunleavy
        # veto-letter form on file.
        "constitutional_cite": ("Under the authority vested in me by Article II, "
                                "Section 15 of the Alaska Constitution, I have "
                                "vetoed the following bill:"),
        "version_designator": version_designator,
        "legal_title":        legal_title,
        "transition":         "",
        "body_paragraphs":    paragraphs,
        # Final standalone closing paragraph — verbatim convention
        # from SB 64, HB 78, HB 26 (akleg.gov/PDF/34/Vetoes).
        "final_statement":    "For these reasons, I have vetoed this bill.",
        "closing":            "Sincerely,",
        "signature_line":     "",
        "signer_name":        "Mike Dunleavy",
        "signer_title":       "Governor",
        "footer":             "Enclosure",
        "model":              resp.get("model") or _VETO_LETTER_MODEL,
        "generated_at":       int(time.time()),
        "input_hash":         h,
        "billnumber":         bn,
    }
    cache[h] = out
    _save_cache()
    return out


def get_cached_veto_letter(billnumber, blue_sheets, briefing_packets,
                           bill_meta, glo_payload, dept_payload,
                           gov_pref="VETO"):
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _veto_letter_input_hash(bn, blue_sheets, briefing_packets,
                                bill_meta, glo_payload, dept_payload,
                                gov_pref)
    return _load_cache().get(h)


# --------------------------------------------------------------------------
# Impacted-departments analysis — identifies the COMPLETE set of agencies
# that materially impact a bill, distinguishing departments that have
# already filed blue sheets from those that haven't but should have.
# Powers the "Departments impacted" row on each card: shows the chase
# list at a glance, with filed/missing visible as a visual property
# rather than something the user has to compute manually.
# --------------------------------------------------------------------------

_IMPACTED_DEPTS_PROMPT_VERSION = "v2-enriched"

_IMPACTED_DEPTS_SYSTEM_PROMPT = """You analyze Alaska bills to identify \
which executive-branch departments and divisions are MATERIALLY IMPACTED \
— meaning they would normally be asked to file a "blue sheet" analysis \
(the formal departmental review submitted to the Governor's Legislative \
Office for veto decisions).

Given the bill's title, the legal description, and the list of \
departments that have ALREADY filed blue sheets, identify the COMPLETE \
set of agencies that impact analysis would reasonably expect on this bill.

Return JSON with this exact shape:

{
  "departments": [
    {"name": "DOR-TAX", "filed": false, "why": "<one short phrase>"},
    {"name": "DOH",     "filed": true,  "why": "<one short phrase>"},
    ...
  ]
}

USE THESE Alaska state agency codes (and only these):

- DOR (Revenue), and divisions: DOR-TAX, DOR-TRS (Treasury / PFD), CSSD
- DOH (Health)
- DFCS (Family and Community Services)
- DEED (Education and Early Development)
- DCCED (Commerce, Community and Economic Development), and divisions: \
DCCED-CBPL (Corporations, Business and Professional Licensing), \
DCCED-DBA (Banking and Securities), DCCED-DCRA (Community and Regional \
Affairs), DCCED-AIDEA (Industrial Development and Export Authority)
- DPS (Public Safety)
- DFG (Fish and Game)
- DEC (Environmental Conservation)
- DOA (Administration), and divisions: DOA-DMV (Motor Vehicles), \
DOA-DOE (Enterprise Tech), DOA-DOR (Retirement and Benefits, also \
sometimes "DOA-Retirement")
- DOC (Corrections)
- DOL (Law)
- DOLWD (Labor and Workforce Development)
- DOT-PF (Transportation and Public Facilities)
- DMVA (Military and Veterans Affairs)
- DNR (Natural Resources)
- UA (University of Alaska) — when bill directly affects UA
- AIDEA — for bills directly affecting the Industrial Development and \
Export Authority
- OMB (Office of Management and Budget) — for appropriations bills
- GLO (Governor's Legislative Office) — never list this; the GLO \
synthesizes departmental input, it doesn't file blue sheets

RULES:

- 3 to 7 departments maximum. Pick only those genuinely material.
- "filed": true only when the dept appears in the FILED AGENCIES list \
provided in the input. Do not guess.
- Prefer divisions over parent departments when the impact is clearly \
on a specific division (e.g., DOR-TAX for a tax bill, not DOR; \
DOA-DMV for a motor-vehicle bill, not DOA).
- "why" should be ONE short phrase (5-15 words), grounded in the bill \
title's actual provisions — not generic boilerplate.
- For pure appropriations bills (titles starting "APPROP:"): list ONLY \
"OMB" with why="appropriations review goes through OMB, not per-dept \
blue sheets".
- For ceremonial / honorific bills (snow classics, anniversary days, \
non-substantive policy statements): return {"departments": []}.
- If the bill amends specific statute sections naming an agency, that \
agency is impacted.

SPECIFIC AGENCY GUIDANCE:

- DOL: Only list DOL when there is a SPECIFIC legal question on this \
bill — constitutional concerns, federal preemption issues, ambiguous \
statutory drafting that could invite litigation. Generic "legal review" \
is NOT sufficient justification. If you list DOL, the "why" must name \
the specific legal concern (e.g., "federal preemption under PL 116-94" \
or "First Amendment concerns about contribution limits").
- OMB: Only list OMB for pure appropriations bills (titles starting \
"APPROP:") OR substantive bills with fiscal impact > $1M that wasn't \
already covered by the originating department.
- DOR-TAX vs DOR: Use DOR-TAX when the bill creates, modifies, or \
repeals a state tax provision. Use DOR (parent) when the impact is on \
revenue administration generally.
- DOR-TRS vs DOR: Use DOR-TRS for any PFD program changes, state \
investment policy, or retirement administration.
- If a parent dept (e.g., DCCED) has already filed a blue sheet, do NOT \
separately list a division (e.g., DCCED-CBPL) UNLESS the bill addresses \
a specific division concern the parent sheet would not have covered. \
Filed-parent typically covers divisions.

USE THE BILL'S ACTUAL PROVISIONS — not just the title — when deciding \
which depts are impacted. The user message will include a neutral \
synthesis of what the bill does, the action_justification text from \
each filed department, and any briefing-packet content. Anchor your \
analysis in that substantive material.

Return ONLY the JSON object. No markdown fence. No preamble."""


def _impacted_depts_input_hash(billnumber, blue_sheets, bill_meta,
                                briefing_packets=None, llm_summary=None):
    parts = [_IMPACTED_DEPTS_PROMPT_VERSION, billnumber.strip()]
    # Filed agencies sorted for hash stability.
    agencies_filed = sorted({
        (s.get("agency") or "").strip().upper()
        for s in (blue_sheets or [])
        if s.get("agency")
    })
    parts.append("FILED|" + "|".join(agencies_filed))
    # Blue-sheet body content (action_justification + description +
    # recommendation). Including these means a fresh OCR pass or a
    # corrected agency analysis invalidates the impacted-depts cache.
    for s in sorted(blue_sheets or [],
                    key=lambda x: (x.get("agency", ""), x.get("filename", ""))):
        parts.append("BS|" + "|".join([
            s.get("agency", ""),
            s.get("recommendation", ""),
            (s.get("description") or "")[:800],
            (s.get("action_justification") or "")[:1200],
        ]))
    # Briefing packet body — same caching invariant.
    for p in sorted(briefing_packets or [], key=lambda x: x.get("filename", "")):
        parts.append("BP|" + (p.get("body_text") or "")[:1200])
    # LLM summary — strongest substantive signal. A re-summarization
    # (new prompt version, new source material) automatically
    # invalidates the impacted-depts cache too.
    if llm_summary:
        parts.append("SUM|" + (llm_summary.get("executive_summary") or ""))
        parts.append("SUM|" + (llm_summary.get("summary") or "")[:1000])
    if bill_meta:
        parts.append(bill_meta.get("latest_version_title") or "")
        parts.append(bill_meta.get("short_title") or "")
    digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()
    return "id:" + digest[:24]


def _build_impacted_depts_user_message(billnumber, blue_sheets, bill_meta,
                                        briefing_packets=None,
                                        llm_summary=None):
    lines = []
    lines.append(f"Bill: {billnumber}")
    if bill_meta:
        if bill_meta.get("short_title"):
            lines.append(f"Short title: {bill_meta['short_title']}")
        if bill_meta.get("latest_version_title"):
            lines.append(f"Legal title: {bill_meta['latest_version_title']}")
    lines.append("")

    # Neutral synthesis of what the bill actually does — the most
    # substantive context available. Lets the LLM identify depts
    # by provision rather than by title pattern alone.
    if llm_summary and (llm_summary.get("summary") or
                        llm_summary.get("executive_summary")):
        lines.append("=== What the bill does (neutral synthesis) ===")
        if llm_summary.get("executive_summary"):
            lines.append(llm_summary["executive_summary"])
            lines.append("")
        if llm_summary.get("summary"):
            lines.append((llm_summary["summary"] or "")[:1500])
        lines.append("")

    # Filed blue sheets WITH their reasoning. Tells the LLM which
    # depts filed, what they said the bill does, and what concerns
    # are already on the record.
    agencies_with_text = [s for s in (blue_sheets or [])
                          if (s.get("agency") or "").strip()
                             and s.get("agency") != "?"]
    if agencies_with_text:
        lines.append("=== Filed blue sheets (departments that have weighed in) ===")
        for s in agencies_with_text:
            agency = s.get("agency", "?")
            rec = (s.get("recommendation") or "no rec").upper()
            just = (s.get("action_justification") or "").strip()
            desc = (s.get("description") or "").strip()
            lines.append(f"--- {agency} (recommends: {rec}) ---")
            if desc:
                lines.append(f"What dept says the bill does: {desc[:400]}")
            if just:
                lines.append(f"Stated reasoning: {just[:500]}")
            lines.append("")
    else:
        lines.append("=== Filed blue sheets: NONE ===")
        lines.append("(no departments have filed blue sheets yet)")
        lines.append("")

    # Briefing-packet body — GLO decision binder content. Surfaces
    # substantive context the blue sheets may have missed.
    if briefing_packets:
        for p in briefing_packets:
            body = (p.get("body_text") or "").strip()
            if body:
                lines.append(f"=== Briefing packet excerpt ({p.get('filename', '')}) ===")
                lines.append(body[:1000])
                lines.append("")

    lines.append("Using the bill's actual provisions above (NOT just the title), "
                 "identify the COMPLETE set of departments materially impacted, "
                 "marking each filed or missing. Follow all rules in the "
                 "system prompt — including the DOL-only-with-specific-legal-"
                 "concern rule and the no-redundant-divisions rule.")
    return "\n".join(lines).strip()


def synthesize_impacted_departments(billnumber, blue_sheets, bill_meta,
                                    briefing_packets=None, llm_summary=None,
                                    *, force=False, timeout=60.0):
    """Return the complete list of departments materially impacted by
    a bill, distinguishing filed from missing. None on failure.

    Enriched inputs (briefing_packets, llm_summary) let the LLM ground
    its analysis in substantive bill content rather than title pattern
    matching alone. Both are optional; absence falls back to the v1
    title-driven behavior."""
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _impacted_depts_input_hash(bn, blue_sheets, bill_meta,
                                   briefing_packets, llm_summary)
    cache = _load_cache()
    if not force and h in cache:
        return cache[h]

    api_key = _read_api_key()
    if not api_key:
        log.warning("impacted_depts.no_api_key bn=%s", bn)
        return None

    user_msg = _build_impacted_depts_user_message(bn, blue_sheets, bill_meta,
                                                   briefing_packets, llm_summary)
    body = {
        "model": _MODEL,
        # v2 enriched-input prompt produces longer, more specific "why"
        # explanations (cites statute sections, names specific
        # provisions). Bumped from 600→1200 after observed mid-JSON
        # truncation on HB 195 — the response had 3 well-formed dept
        # entries and was cut off in the middle of the 4th.
        "max_tokens": 1200,
        "system": _IMPACTED_DEPTS_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(body).encode("utf-8"),
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
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            body_text = ""
        log.warning("impacted_depts.http_error bn=%s status=%s body=%r",
                    bn, e.code, body_text)
        return None
    except Exception as e:
        log.warning("impacted_depts.error bn=%s err=%r", bn, e)
        return None

    text = ""
    for chunk in resp.get("content") or []:
        if chunk.get("type") == "text":
            text += chunk.get("text", "")
    text = text.strip()
    if text.startswith("```"):
        text = text.lstrip("`").lstrip()
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    try:
        parsed = _forgiving_json_loads(text)
    except json.JSONDecodeError:
        log.warning("impacted_depts.bad_json bn=%s raw=%r", bn, text[:300])
        return None

    # Defensive normalization: keep only well-formed entries, drop unknown
    # keys, normalize types, cap at 8 to defend against runaway model output.
    raw_list = parsed.get("departments") or []
    filed_set = {
        (s.get("agency") or "").strip().upper()
        for s in (blue_sheets or [])
        if s.get("agency") and s.get("agency") != "?"
    }
    depts = []
    seen = set()
    for d in raw_list[:8]:
        name = str(d.get("name", "")).strip().upper()
        if not name or name in seen:
            continue
        seen.add(name)
        why = str(d.get("why", "")).strip()
        # Trust the LLM's "filed" flag but cross-check against actual
        # filed list. Authoritative source: blue_sheets data, not LLM.
        is_filed = name in filed_set
        depts.append({
            "name":  name,
            "filed": is_filed,
            "why":   why[:140],
        })

    out = {
        "departments":  depts,
        "model":        _MODEL,
        "generated_at": int(time.time()),
        "input_hash":   h,
        "billnumber":   bn,
    }
    cache[h] = out
    _save_cache()
    return out


def get_cached_impacted_departments(billnumber, blue_sheets, bill_meta,
                                     briefing_packets=None, llm_summary=None):
    bn = (billnumber or "").strip()
    if not bn:
        return None
    h = _impacted_depts_input_hash(bn, blue_sheets, bill_meta,
                                   briefing_packets, llm_summary)
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
