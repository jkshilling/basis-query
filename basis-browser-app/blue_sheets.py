"""Blue-sheet index: scan basis-browser-app/blue_sheets/ and map
each PDF/DOCX file back to a canonical billnumber.

A "blue sheet" is a one-page legislative analysis attached to a
bill — the term comes from the (historically blue) paper they were
printed on. Multiple state agencies can submit separate blue sheets
on the same bill (e.g. HB 133 has analyses from DOH, DPS, and
DCCED), so each bill can have N sheets.

Filename matching is intentionally forgiving: the first occurrence
of a bill prefix (HB/SB/HJR/SJR/HCR/SCR/HR/SR/HSCR/SSCR) followed
by digits wins, optionally preceded by a CS/HCS/SCS committee-
substitute marker. Leading zeros stripped.

When possible the filename also reveals which agency wrote the
sheet (DOH, DPS, DCCED, etc.) and the date it was filed — both
get pulled out for display.
"""

import os
import re
import time

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blue_sheets")

# Permissible bill-number prefixes, longest first so "HSCR" matches
# before "HCR" or "SR".
_PREFIXES = ("HSCR", "SSCR", "HJR", "SJR", "HCR", "SCR", "HB", "SB", "HR", "SR")
_PREFIX_PATTERN = "|".join(_PREFIXES)

_BILL_RE = re.compile(
    r"(?:CS|HCS|SCS|SCS\s?CS|HCS\s?CS)?"
    r"(" + _PREFIX_PATTERN + r")"
    r"\s*0*(\d+)",
    re.IGNORECASE,
)

_EXT_RE = re.compile(r"\.(pdf|docx?)$", re.IGNORECASE)

# Known state-agency abbreviations that appear in filenames. We try
# to surface these on the UI for context. Order longest-first so
# "DCCED" matches before "DOA". Not exhaustive — unknown abbreviations
# are simply not labeled.
_AGENCY_PATTERNS = (
    ("DCCED", r"\bDCCED\b"),
    ("DFCS",  r"\bDFCS\b"),
    ("AIDEA", r"\bAIDEA\b"),
    ("AMCO",  r"\bAMCO\b"),
    ("ARRC",  r"\bARRC\b"),
    ("DOH",   r"\bDOH\b"),
    ("DPS",   r"\bDPS\b"),
    ("DOA",   r"\bDOA\b"),
    ("DMV",   r"\bDMV\b"),
    ("DOC",   r"\bDOC\b"),
    ("DOR",   r"\bDOR\b"),
    ("DEC",   r"\bDEC\b"),
    ("DNR",   r"\bDNR\b"),
    ("DOL",   r"\bDOL\b"),
    ("DOLWD", r"\bDOLWD\b"),
)

# Date patterns like 5.18.26, 05-18-26, 5/18/2026
_DATE_RE = re.compile(
    r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})\b",
)

# In-memory cache
_cache_value = None
_cache_time = 0.0
_CACHE_TTL = 60.0


def _extract_billnumber(filename):
    if not _EXT_RE.search(filename):
        return None
    m = _BILL_RE.search(filename)
    if not m:
        return None
    prefix = m.group(1).upper()
    number = str(int(m.group(2)))
    return f"{prefix} {number}"


def _extract_agency(filename):
    for label, pat in _AGENCY_PATTERNS:
        if re.search(pat, filename, re.IGNORECASE):
            return label
    return ""


def _extract_date(filename):
    """Return a human-readable date string (e.g. 'May 18') if the
    filename contains an embedded mm/dd/yy or similar pattern."""
    m = _DATE_RE.search(filename)
    if not m:
        return ""
    try:
        mm, dd, yy = (int(x) for x in m.groups())
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            return ""
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return f"{months[mm - 1]} {dd}"
    except (ValueError, IndexError):
        return ""


def _scan():
    """Return {canonical_billnumber: [sheet_meta, ...]} — list per
    bill, since multiple agencies can each file a sheet on the same
    bill. Each meta dict has: filename, agency, date, label."""
    out = {}
    if not os.path.isdir(_DIR):
        return out
    for fn in sorted(os.listdir(_DIR)):
        bn = _extract_billnumber(fn)
        if not bn:
            continue
        agency = _extract_agency(fn)
        date = _extract_date(fn)
        # Human-friendly label: "DOH · May 12" / "DPS" / "May 18" / ""
        label_parts = [p for p in (agency, date) if p]
        label = " · ".join(label_parts)
        out.setdefault(bn, []).append({
            "filename": fn,
            "agency": agency,
            "date": date,
            "label": label,
        })
    # De-dup within each bill: if two entries have identical (agency,
    # date) keep only the first by sorted filename. Catches the
    # "HB23 (1).docx" downloaded-twice case.
    for bn, lst in out.items():
        seen = set()
        unique = []
        for sheet in lst:
            key = (sheet["agency"], sheet["date"])
            # If both agency and date are blank, dedupe by filename
            # base (catches "name.docx" vs "name (1).docx").
            if not key[0] and not key[1]:
                base = re.sub(r"\s*\(\d+\)", "", sheet["filename"]).lower()
                key = ("__nokey__", base)
            if key in seen:
                continue
            seen.add(key)
            unique.append(sheet)
        out[bn] = unique
    return out


def index():
    """Cached {billnumber: [sheet_meta, ...]} mapping."""
    global _cache_value, _cache_time
    now = time.monotonic()
    if _cache_value is not None and (now - _cache_time) < _CACHE_TTL:
        return _cache_value
    _cache_value = _scan()
    _cache_time = now
    return _cache_value


def sheets_for(billnumber):
    """Return list of sheet_meta dicts for a bill (empty if none)."""
    return index().get((billnumber or "").strip(), [])


def abs_path(filename):
    """Resolve a sanitized filename to an absolute path inside the
    blue_sheets/ dir. Returns None if the filename would escape the
    directory (defensive — prevents path traversal)."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    candidate = os.path.normpath(os.path.join(_DIR, filename))
    if not candidate.startswith(_DIR + os.sep):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate
