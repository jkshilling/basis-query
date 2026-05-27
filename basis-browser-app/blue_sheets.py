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

# Date patterns. Two forms:
#   ISO:      YYYY-MM-DD   (e.g. 2026-05-14)
#   American: M-D-YY[YY]   (e.g. 5.18.26, 05-18-26, 5/18/2026)
_DATE_RE_ISO = re.compile(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})")
_DATE_RE_AMR = re.compile(r"\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})\b")

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


# Cache extracted agency-from-content per-file to avoid re-cracking
# PDFs on every page load. Keyed by (filename, mtime).
_content_agency_cache = {}


def _extract_agency_from_content(filename):
    """Open the file and look for an agency code in its first-page
    text. Used when the filename doesn't reveal the department —
    sheets usually show e.g. 'DEPARTMENT OF ADMINISTRATION (DOA)' in
    the header. Falls back to '' on any failure (missing pypdf, OCR-
    only PDF, weird DOCX, etc.)."""
    path = os.path.join(_DIR, filename)
    if not os.path.isfile(path):
        return ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (filename, mtime)
    if key in _content_agency_cache:
        return _content_agency_cache[key]

    text = ""
    try:
        if filename.lower().endswith(".pdf"):
            import pypdf
            reader = pypdf.PdfReader(path)
            # First page only — agency name lives in the header. PDF
            # text extraction is the slowest part of this pipeline, so
            # we want the minimum.
            if reader.pages:
                text = reader.pages[0].extract_text() or ""
        elif filename.lower().endswith((".docx", ".doc")):
            # DOCX is a zip with XML inside; extract /word/document.xml
            # and pull text without a heavy dep.
            import zipfile, re as _re
            try:
                with zipfile.ZipFile(path) as zf:
                    with zf.open("word/document.xml") as f:
                        raw = f.read().decode("utf-8", errors="replace")
                # Strip XML tags
                text = _re.sub(r"<[^>]+>", " ", raw)
            except (KeyError, zipfile.BadZipFile):
                pass
    except Exception:
        # Don't let an unreadable file break the whole index.
        pass

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


def _extract_date(filename):
    """Return a human-readable date string (e.g. 'May 18') if the
    filename contains a recognizable date pattern. Tries ISO first
    (YYYY-MM-DD) then American (M-D-YY)."""
    # ISO format
    m = _DATE_RE_ISO.search(filename)
    if m:
        try:
            year, mm, dd = (int(x) for x in m.groups())
            if 2020 <= year <= 2100 and 1 <= mm <= 12 and 1 <= dd <= 31:
                return f"{_MONTHS[mm - 1]} {dd}"
        except (ValueError, IndexError):
            pass
    # American format — but skip if the first group looks like a
    # 4-digit year (would have matched ISO above; only get here if
    # ISO failed for some reason).
    m = _DATE_RE_AMR.search(filename)
    if m:
        try:
            mm, dd, yy = (int(x) for x in m.groups())
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return f"{_MONTHS[mm - 1]} {dd}"
        except (ValueError, IndexError):
            pass
    return ""


# Boilerplate words to strip when synthesizing a fallback label from
# an unlabeled filename. Case-insensitive.
_LABEL_NOISE_RE = re.compile(
    r"\b(?:blue\s*sheet|BS|sheet|2025|2026|signed|revised|updated|"
    r"docx|pdf)\b",
    re.IGNORECASE,
)


def _fallback_label(filename, billnumber):
    """When agency + date extraction yields nothing, build a short
    human-readable label from the filename. Strip the extension, the
    bill-number reference, common boilerplate ('Blue Sheet', '2026',
    'Revised'), and any extra punctuation. If nothing meaningful
    remains, return ''."""
    name = re.sub(r"\.(pdf|docx?)$", "", filename, flags=re.IGNORECASE)
    # Strip the bill-number reference (with or without leading zeros).
    bn_prefix, bn_num = billnumber.split()
    name = re.sub(
        rf"(?:CS|HCS|SCS)?{bn_prefix}\s*0*{bn_num}\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    # Strip boilerplate words.
    name = _LABEL_NOISE_RE.sub("", name)
    # Collapse separators and trim.
    name = re.sub(r"[-_().]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -_")
    # Discard residues that are too short or pure punctuation/digits
    # (e.g. "for", "1", "-") — they're noise, not information.
    if len(name) < 4 or name.isdigit():
        return ""
    # Truncate long synthesized labels.
    if len(name) > 28:
        name = name[:25].rstrip() + "…"
    return name


def _scan():
    """Return {canonical_billnumber: [sheet_meta, ...]} — list per
    bill, since multiple agencies can each file a sheet on the same
    bill. Each meta dict has: filename, agency, date, label."""
    out = {}
    if not os.path.isdir(_DIR):
        return out
    for fn in sorted(os.listdir(_DIR)):
        bns = _extract_billnumbers(fn)
        if not bns:
            continue
        agency = _extract_agency(fn) or _extract_agency_from_content(fn)
        date = _extract_date(fn)
        # Index the same file under EVERY bill it references. Agency
        # blue sheets sometimes cover multiple bills (e.g.
        # "UA Blue Sheet HB 10 and HB 176.pdf" → both HB 10 and HB 176).
        for bn in bns:
            label_parts = [p for p in (agency, date) if p]
            label = " · ".join(label_parts)
            if not label:
                label = _fallback_label(fn, bn) or "Blue sheet"
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
