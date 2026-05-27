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
_SYSTEM_PROMPT_VERSION = "v8-no-bn-repeat"

_SYSTEM_PROMPT = """You are writing analysis for a veto-decision-support dashboard \
about Alaska bills. For each bill, return a JSON object with TWO fields:

{
  "executive_summary": "<one tight sentence, 12-22 words, in active voice, saying \
WHAT THE BILL DOES. Plain English. No filler openers. No 'Enacted under X', no \
'This bill', no 'Under this legislation'. \
DO NOT start with the bill number ('HB 36 creates...'); the card already \
displays the bill number prominently right next to this summary, so repeating \
it wastes the slot. Start with the substantive change as the subject. \
Examples that are GOOD: \
  'Creates a new treatment foster home license category and requires judicial \
review of foster-child psychiatric hospitalizations within 7 days.' \
  'Establishes gold and silver coin and bullion as legal tender; prohibits \
municipal sales tax on specie exchanges.' \
  'Joins Alaska to the Occupational Therapy Licensure Compact, allowing OTs \
and OTAs to practice across member states without separate Alaska licensure.'>",

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
            desc = s.get("description") or "(no analytical text extracted from this sheet)"
            lines.append(f"=== {agency} blue sheet — recommends: {rec} ===")
            lines.append(desc.strip())
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
        parsed = json.loads(raw_text)
        exec_summary = str(parsed.get("executive_summary", "")).strip()
        full_summary = str(parsed.get("summary", "")).strip()
    except json.JSONDecodeError:
        # Fallback: treat the whole response as the summary, leave exec
        # summary empty so the template extracts from the first sentence.
        log.warning("summarizer.json_parse_failed bn=%s raw=%r", bn, raw_text[:300])
        exec_summary = ""
        full_summary = raw_text

    if not full_summary:
        log.warning("summarizer.empty_summary bn=%s", bn)
        return None

    usage = resp.get("usage") or {}
    out = {
        "executive_summary": exec_summary,
        "summary":           full_summary,
        "model":             _MODEL,
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

_RATIONALE_PROMPT_VERSION = "v1"

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
        "model": _MODEL,
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
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("rationale.bad_json bn=%s raw=%r", bn, text[:300])
        return None

    out = {
        "glo_rationale":   str(parsed.get("glo_rationale", "")).strip(),
        "dept_rationales": parsed.get("dept_rationales", []) or [],
        "model":           _MODEL,
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
        parsed = json.loads(text)
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
