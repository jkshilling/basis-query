"""Legal-analyses index — parallels blue_sheets.py for the
basis-browser-app/legal_analyses/ folder.

Legal analyses are written by Legislative Legal Services (LLS) and
the Department of Law (DOL / AG). Unlike blue sheets (executive-
branch agency analyses on bills only), legal analyses apply to
both bills AND resolutions — especially HJRs/SJRs with
constitutional questions.

Label shape is ALWAYS 'SOURCE · MON DD' for chip rendering, just
like blue sheets. Date cascades filename → PDF content → file
mtime; source cascades filename → PDF content → '?'.
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

# Per-file content caches keyed by (filename, mtime).
_content_text_cache = {}
_content_source_cache = {}
_content_date_cache = {}


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


def _read_first_page_text(filename):
    """First-page text of a PDF/DOCX. Cached per (filename, mtime)."""
    path = os.path.join(_DIR, filename)
    if not os.path.isfile(path):
        return ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (filename, mtime)
    if key in _content_text_cache:
        return _content_text_cache[key]

    text = ""
    try:
        if filename.lower().endswith(".pdf"):
            import pypdf
            reader = pypdf.PdfReader(path)
            if reader.pages:
                text = reader.pages[0].extract_text() or ""
        elif filename.lower().endswith((".docx", ".doc")):
            import zipfile
            try:
                with zipfile.ZipFile(path) as zf:
                    with zf.open("word/document.xml") as f:
                        raw = f.read().decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", " ", raw)
            except (KeyError, zipfile.BadZipFile):
                pass
    except Exception:
        pass

    _content_text_cache[key] = text
    return text


def _extract_source_from_content(filename):
    path = os.path.join(_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (filename, mtime)
    if key in _content_source_cache:
        return _content_source_cache[key]

    text = _read_first_page_text(filename)
    found = ""
    if text:
        upper = text.upper()
        for label, _ in _SOURCE_PATTERNS:
            if re.search(rf"\b{label}\b", upper):
                found = label
                break
        if not found:
            long_names = (
                ("LLS", r"LEGISLATIVE\s+LEGAL\s+SERVICES"),
                ("DOL", r"DEPARTMENT\s+OF\s+LAW"),
                ("AG",  r"ATTORNEY\s+GENERAL"),
            )
            for label, pat in long_names:
                if re.search(pat, upper):
                    found = label
                    break

    _content_source_cache[key] = found
    return found


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


def _extract_date_from_content(filename):
    path = os.path.join(_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (filename, mtime)
    if key in _content_date_cache:
        return _content_date_cache[key]

    text = _read_first_page_text(filename)
    found = _extract_date_from_text(text) if text else ""
    _content_date_cache[key] = found
    return found


def _mtime_date(filename):
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
        bn = _extract_billnumber(fn)
        if not bn:
            continue
        source = (
            _extract_source(fn)
            or _extract_source_from_content(fn)
            or "?"
        )
        date = (
            _extract_date(fn)
            or _extract_date_from_content(fn)
            or _mtime_date(fn)
        )
        label = f"{source} · {date}" if date else source
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
