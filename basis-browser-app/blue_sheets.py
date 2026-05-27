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
    ("DOLWD", r"\bDOLWD\b"),
    ("AIDEA", r"\bAIDEA\b"),
    ("DMVA",  r"\bDMVA\b"),
    ("DFCS",  r"\bDFCS\b"),
    ("DEED",  r"\bDEED\b"),
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
    ("DFG",   r"\bDFG\b"),
    ("UA",    r"\bUA\b"),
)

# Date patterns. Three forms:
#   ISO:        YYYY-MM-DD            (e.g. 2026-05-14)
#   American:   M-D-YY[YY]            (e.g. 5.18.26, 05-18-26, 5/18/2026)
#   Long month: DD MMM YY[YY]         (e.g. 08 MAY 26)
_DATE_RE_ISO = re.compile(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})")
_DATE_RE_AMR = re.compile(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})\b")
_DATE_RE_LONG = re.compile(
    r"\b(\d{1,2})\s+"
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s+"
    r"(\d{2,4})\b",
    re.IGNORECASE,
)

# In-memory cache
_cache_value = None
_cache_time = 0.0
_CACHE_TTL = 60.0


def _extract_billnumber(filename):
    """Single-bill extractor — used for the simple case where a file
    relates to exactly one bill. Returns the first match or None."""
    if not _EXT_RE.search(filename):
        return None
    m = _BILL_RE.search(filename)
    if not m:
        return None
    prefix = m.group(1).upper()
    number = str(int(m.group(2)))
    return f"{prefix} {number}"


def _extract_billnumbers(filename):
    """Multi-bill extractor — finds every bill reference in a single
    filename and de-dupes. Used so that a sheet titled e.g.
    'UA Blue Sheet HB 10 and HB 176.pdf' indexes under BOTH HB 10
    and HB 176."""
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


def _extract_agency(filename):
    for label, pat in _AGENCY_PATTERNS:
        if re.search(pat, filename, re.IGNORECASE):
            return label
    return ""


# Cache extracted first-page text per-file. Both agency and date
# extraction read the same first-page bytes — cache once, use twice.
# Keyed by (filename, mtime) so a re-saved file invalidates.
_content_text_cache = {}
_content_agency_cache = {}


def _read_first_page_text(filename):
    """Open a PDF/DOCX in blue_sheets/ and return its first-page text.
    Empty string on any failure (missing pypdf, image-only PDF, weird
    DOCX). Cached per (filename, mtime)."""
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


def _extract_agency_from_content(filename):
    """Look for an agency code in the first-page text. Used when the
    filename doesn't reveal the department — sheets typically show
    'DEPARTMENT OF ADMINISTRATION (DOA)' in the header."""
    path = os.path.join(_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (filename, mtime)
    if key in _content_agency_cache:
        return _content_agency_cache[key]

    text = _read_first_page_text(filename)
    found = ""
    if text:
        # Match the same agency tokens, plus their long names where
        # the abbreviation might not appear (e.g. "DEPARTMENT OF
        # ADMINISTRATION" — DOA — sometimes spelled out).
        upper = text.upper()
        for label, _ in _AGENCY_PATTERNS:
            if re.search(rf"\b{label}\b", upper):
                found = label
                break
        # Long-name fallback table.
        if not found:
            long_names = (
                ("DOA",   r"DEPARTMENT\s+OF\s+ADMINISTRATION"),
                ("DOC",   r"DEPARTMENT\s+OF\s+CORRECTIONS"),
                ("DEC",   r"DEPARTMENT\s+OF\s+ENVIRONMENTAL\s+CONSERVATION"),
                ("DCCED", r"DEPARTMENT\s+OF\s+COMMERCE"),
                ("DEED",  r"DEPARTMENT\s+OF\s+EDUCATION"),
                ("DOH",   r"DEPARTMENT\s+OF\s+HEALTH"),
                ("DFCS",  r"DIVISION\s+OF\s+FAMILY\s+AND\s+COMMUNITY\s+SERVICES"),
                ("DPS",   r"DEPARTMENT\s+OF\s+PUBLIC\s+SAFETY"),
                ("DMV",   r"DIVISION\s+OF\s+MOTOR\s+VEHICLES"),
                ("DOR",   r"DEPARTMENT\s+OF\s+REVENUE"),
                ("DNR",   r"DEPARTMENT\s+OF\s+NATURAL\s+RESOURCES"),
                ("DOL",   r"DEPARTMENT\s+OF\s+LABOR"),
                ("DOLWD", r"DEPARTMENT\s+OF\s+LABOR\s+AND\s+WORKFORCE"),
                ("DFG",   r"DEPARTMENT\s+OF\s+FISH\s+AND\s+GAME"),
                ("DMVA",  r"DEPARTMENT\s+OF\s+MILITARY"),
                ("UA",    r"UNIVERSITY\s+OF\s+ALASKA"),
            )
            for label, pat in long_names:
                if re.search(pat, upper):
                    found = label
                    break

    _content_agency_cache[key] = found
    return found


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_LOOKUP = {m.upper(): i + 1 for i, m in enumerate(_MONTHS)}


def _extract_date_from_text(text):
    """Try every date pattern (ISO, American, 'DD MMM YY') against the
    given text and return 'Mon DD' on first hit, else ''."""
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


# Cache extracted date-from-content per-file. The PDF content cache
# already costs nothing because content extraction for agency reads
# the same first-page text — we just stash both results.
_content_date_cache = {}


def _extract_date_from_content(filename):
    """Look for a date pattern in the PDF/DOCX first-page text. Many
    blue sheets have a 'Date: 5/12/26' or '5/12/2026' line near the
    top even when the filename omits it."""
    path = os.path.join(_DIR, filename)
    if not os.path.isfile(path):
        return ""
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
    """File mtime as a 'Mon DD' string — last-resort date source so
    every chip always has a date component."""
    path = os.path.join(_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    import datetime as _dt
    d = _dt.date.fromtimestamp(mtime)
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _scan():
    """Return {canonical_billnumber: [sheet_meta, ...]} — list per
    bill, since multiple agencies can each file a sheet on the same
    bill. Each meta dict has: filename, agency, date, label.

    Label shape is ALWAYS 'AGENCY · MON DD' — no exceptions. Date
    cascades filename → PDF content → file mtime so every chip has
    one. Agency cascades filename → PDF content → '?' so every chip
    is comparable shape."""
    out = {}
    if not os.path.isdir(_DIR):
        return out
    for fn in sorted(os.listdir(_DIR)):
        bns = _extract_billnumbers(fn)
        if not bns:
            continue
        agency = (
            _extract_agency(fn)
            or _extract_agency_from_content(fn)
            or "?"
        )
        date = (
            _extract_date(fn)
            or _extract_date_from_content(fn)
            or _mtime_date(fn)
        )
        label = f"{agency} · {date}" if date else agency
        # Index the same file under EVERY bill it references. Agency
        # blue sheets sometimes cover multiple bills (e.g.
        # "UA Blue Sheet HB 10 and HB 176.pdf" → both HB 10 and HB 176).
        for bn in bns:
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
