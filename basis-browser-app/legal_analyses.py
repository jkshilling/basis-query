"""Legal-analyses index — parallels blue_sheets.py for the
basis-browser-app/legal_analyses/ folder.

Legal analyses are written by Legislative Legal Services (LLS) and
the Department of Law (DOL / AG). Unlike blue sheets (executive-
branch agency analyses on bills only), legal analyses apply to
both bills AND resolutions — especially HJRs/SJRs with
constitutional questions.

Filename matching is the same forgiving regex used for blue sheets:
the first bill prefix + digits anywhere in the name wins, optionally
preceded by a CS/HCS/SCS committee-substitute marker. Leading zeros
stripped.
"""

import os
import re
import time

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legal_analyses")

_PREFIXES = ("HSCR", "SSCR", "HJR", "SJR", "HCR", "SCR", "HB", "SB", "HR", "SR")
_PREFIX_PATTERN = "|".join(_PREFIXES)

_BILL_RE = re.compile(
    r"(?:CS|HCS|SCS|SCS\s?CS|HCS\s?CS)?"
    r"(" + _PREFIX_PATTERN + r")"
    r"\s*0*(\d+)",
    re.IGNORECASE,
)

_EXT_RE = re.compile(r"\.(pdf|docx?)$", re.IGNORECASE)

# Legal-source labels — different patterns than blue-sheet agencies.
# LLS = Legislative Legal Services; DOL/AG/LAW = Department of Law /
# Attorney General. Order longest-first.
_SOURCE_PATTERNS = (
    ("LLS", r"\bLLS\b"),
    ("DOL", r"\bDOL\b"),
    ("AG",  r"\bAG\b"),
    ("LAW", r"\bLAW\b"),
)

_DATE_RE = re.compile(
    r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})\b",
)

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


def _extract_source(filename):
    for label, pat in _SOURCE_PATTERNS:
        if re.search(pat, filename, re.IGNORECASE):
            return label
    return ""


def _extract_date(filename):
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
    out = {}
    if not os.path.isdir(_DIR):
        return out
    for fn in sorted(os.listdir(_DIR)):
        bn = _extract_billnumber(fn)
        if not bn:
            continue
        source = _extract_source(fn)
        date = _extract_date(fn)
        label_parts = [p for p in (source, date) if p]
        label = " · ".join(label_parts)
        out.setdefault(bn, []).append({
            "filename": fn,
            "source": source,
            "date": date,
            "label": label,
        })
    # Dedup within bill: same (source, date) → keep first.
    for bn, lst in out.items():
        seen = set()
        unique = []
        for sheet in lst:
            key = (sheet["source"], sheet["date"])
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
    global _cache_value, _cache_time
    now = time.monotonic()
    if _cache_value is not None and (now - _cache_time) < _CACHE_TTL:
        return _cache_value
    _cache_value = _scan()
    _cache_time = now
    return _cache_value


def analyses_for(billnumber):
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
