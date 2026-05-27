"""Briefing-packet index — parallels blue_sheets.py and
legal_analyses.py for the basis-browser-app/briefing_packets/ folder.

A briefing packet is a curated decision-support document — typically
the Governor's Legislative Office (GLO) compiling the departmental
blue sheets, legal review, and political analysis into one binder
the Governor or chief of staff can read in one sitting.

Filename matching is the same forgiving regex used for blue sheets
and legal analyses: the first bill prefix + digits anywhere in the
name wins, optionally preceded by a CS/HCS/SCS marker. Leading
zeros stripped. Multi-bill files (e.g. "Brief HB 10 and HB 176.pdf")
get indexed under every bill referenced.

No content parsing yet — the chip just shows a date label. If
briefing packets carry structured fields (a final recommendation,
key issues list, etc.) we can add extraction later, same way we
mine blue sheets for SIGN/VETO/LWOS + "What does this Bill do?".
"""

from __future__ import annotations

import os
import re
import time

_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "briefing_packets",
)

_PREFIXES = ("HSCR", "SSCR", "HJR", "SJR", "HCR", "SCR", "HB", "SB", "HR", "SR")
_PREFIX_PATTERN = "|".join(_PREFIXES)

_BILL_RE = re.compile(
    r"(?:CS|HCS|SCS|SCS\s?CS|HCS\s?CS)?"
    r"(" + _PREFIX_PATTERN + r")"
    r"\s*0*(\d+)",
    re.IGNORECASE,
)

_EXT_RE = re.compile(r"\.(pdf|docx?)$", re.IGNORECASE)

# Same three date patterns we use elsewhere.
_DATE_RE_ISO = re.compile(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})")
_DATE_RE_AMR = re.compile(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})\b")
_DATE_RE_LONG = re.compile(
    r"\b(\d{1,2})\s+"
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s+"
    r"(\d{2,4})\b",
    re.IGNORECASE,
)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_LOOKUP = {m.upper(): i + 1 for i, m in enumerate(_MONTHS)}

_cache_value = None
_cache_time = 0.0
_CACHE_TTL = 60.0

# Per-file DOL-review extraction cache, keyed by (filename, mtime).
_dol_cache = {}


def _read_docx_text(filename):
    """Pull plain text out of a DOCX in the briefing_packets folder.
    Returns empty string on any failure."""
    import zipfile
    path = os.path.join(_DIR, filename)
    if not os.path.isfile(path):
        return ""
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open("word/document.xml") as f:
                raw = f.read().decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", raw)
    except (KeyError, zipfile.BadZipFile, OSError):
        return ""


# Briefing packets carry a "DEPARTMENT OF LAW BILL REVIEW" section
# (all 27 packets we audited had one). Extract that section text so
# the dashboard can surface DOL's legal review as a first-class
# signal alongside blue sheets and legal_analyses files.
#
# Header format in DOCX-flattened text is loose — "DEPARTMENT" and
# "OF LAW BILL REVIEW" can be separated by big runs of whitespace
# (from collapsed multi-cell table layout). Use \s+ everywhere.
# The section ends at the next ALL-CAPS section header (typical
# pattern in these packets: "PROS:", "CONS:", "FISCAL ANALYSIS:",
# "SECTIONAL ANALYSIS:", "ADMINISTRATION POSITION:", etc.) or at
# the end of the document.
# Known section headers that follow DOL BILL REVIEW in Alaska
# briefing-paper template. Curated from a scan across all 27
# packets. Using a generic "any ALL-CAPS heading" pattern caused
# false-positive matches inside the DOL text body (e.g. "TBD
# DEPARTMENT RECOMMENDATION" got parsed as a heading at offset 0,
# capturing zero chars of DOL content). The curated list is more
# robust.
_DOL_NEXT_HEADINGS = [
    r"DEPARTMENT\s+RECOMMENDATION",
    r"STATUES?\s+AFFECTED",      # 'STATUES' is a recurring template typo
    r"STATUTES\s+REPEALED",
    r"DUE\s+TO\s+THE\s+LEGISLATURE\s+BY",
    r"SIGNING\s+CEREMONY\s+SUGGESTIONS",
    r"FISCAL\s+IMPACT",
    r"BILL\s+SPECIFICS",
    r"HOUSE\s+ON\s+PASSAGE",
    r"SENATE\s+ON\s+PASSAGE",
]
_DOL_RE = re.compile(
    r"DEPARTMENT OF LAW BILL REVIEW\s*:?\s*"
    r"(.*?)"
    r"(?=(?:" + "|".join(_DOL_NEXT_HEADINGS) + r")\s*:|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _normalize_ws(s):
    """Collapse all whitespace runs (including newlines) into single
    spaces. Briefing-packet DOCX text comes with massive runs of
    spaces from collapsed table cells; the section-detection regex
    can't reliably look ahead through those runs."""
    return re.sub(r"\s+", " ", s).strip()


def _extract_dol_review(filename):
    """Return the body text of the 'DEPARTMENT OF LAW BILL REVIEW'
    section from one packet, or '' if not present / unparseable.
    Caches per (filename, mtime)."""
    path = os.path.join(_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (filename, mtime)
    if key in _dol_cache:
        return _dol_cache[key]

    raw = _read_docx_text(filename) if filename.lower().endswith(".docx") else ""
    out = ""
    if raw:
        # Normalize whitespace FIRST so the section-boundary lookahead
        # works regardless of DOCX table-cell flattening.
        text = _normalize_ws(raw)
        m = _DOL_RE.search(text)
        if m:
            body = m.group(1).strip()
            # Clean some common boilerplate artifacts.
            body = re.sub(r"^\s*[:.]+\s*", "", body)
            if len(body) > 4000:
                body = body[:3950].rsplit(" ", 1)[0] + "…"
            out = body
    _dol_cache[key] = out
    return out


def _extract_billnumbers(filename):
    """Find every bill reference in the filename; dedupe in order."""
    if not _EXT_RE.search(filename):
        return []
    seen = []
    for m in _BILL_RE.finditer(filename):
        prefix = m.group(1).upper()
        number = str(int(m.group(2)))
        bn = f"{prefix} {number}"
        if bn not in seen:
            seen.append(bn)
    return seen


def _extract_date_from_text(text):
    if not text:
        return ""
    m = _DATE_RE_ISO.search(text)
    if m:
        try:
            year, mm, dd = (int(x) for x in m.groups())
            if 2020 <= year <= 2100 and 1 <= mm <= 12 and 1 <= dd <= 31:
                return f"{_MONTHS[mm - 1]} {dd}"
        except (ValueError, IndexError):
            pass
    m = _DATE_RE_AMR.search(text)
    if m:
        try:
            mm, dd, yy = (int(x) for x in m.groups())
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return f"{_MONTHS[mm - 1]} {dd}"
        except (ValueError, IndexError):
            pass
    m = _DATE_RE_LONG.search(text)
    if m:
        try:
            dd = int(m.group(1))
            mm = _MONTH_LOOKUP.get(m.group(2).upper()[:3], 0)
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return f"{_MONTHS[mm - 1]} {dd}"
        except (ValueError, IndexError):
            pass
    return ""


def _extract_date(filename):
    return _extract_date_from_text(filename)


def _mtime_date(filename):
    """File mtime as a 'Mon DD' string — last-resort date so every
    chip carries one consistently."""
    path = os.path.join(_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    import datetime as _dt
    d = _dt.date.fromtimestamp(mtime)
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _scan():
    out = {}
    if not os.path.isdir(_DIR):
        return out
    for fn in sorted(os.listdir(_DIR)):
        bns = _extract_billnumbers(fn)
        if not bns:
            continue
        date = _extract_date(fn) or _mtime_date(fn)
        # Label shape: always just "Brief". Date is kept on the meta
        # dict for dedup keying within a bill, but NOT shown in the
        # chip label — user wants uniform chip text across packets.
        label = "Brief"
        # Department of Law bill-review text (every packet we've seen
        # contains one). Cached per file.
        dol_text = _extract_dol_review(fn)
        # Full normalized body — fed to the LLM summarizer as
        # supplementary context when blue sheets are sparse or
        # image-only. Truncated to keep token cost predictable.
        full_text = ""
        if fn.lower().endswith(".docx"):
            full_text = _normalize_ws(_read_docx_text(fn) or "")
            if len(full_text) > 6000:
                full_text = full_text[:5980].rsplit(" ", 1)[0] + "…"
        for bn in bns:
            out.setdefault(bn, []).append({
                "filename":   fn,
                "date":       date,
                "label":      label,
                "dol_review": dol_text,
                "body_text":  full_text,
            })
    # Dedupe within each bill by date (rare — typically one packet per bill).
    for bn, lst in out.items():
        seen = set()
        unique = []
        for entry in lst:
            key = entry["date"] or entry["filename"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        out[bn] = unique
    return out


def index():
    global _cache_value, _cache_time
    now = time.monotonic()
    if _cache_value is not None and (now - _cache_time) < _CACHE_TTL:
        return _cache_value
    _cache_value = _scan()
    _cache_time = now
    return _cache_value


def packets_for(billnumber):
    return index().get((billnumber or "").strip(), [])


def abs_path(filename):
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    candidate = os.path.normpath(os.path.join(_DIR, filename))
    if not candidate.startswith(_DIR + os.sep):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate
