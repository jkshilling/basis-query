"""Blue-sheet index: scan basis-browser-app/blue_sheets/ and map
each PDF/DOCX file back to a canonical billnumber.

A "blue sheet" is a one-page legislative analysis attached to a
bill — the term comes from the (historically blue) paper they were
printed on. Drop a file in the folder and the Governor's Desk page
picks it up automatically.

Filename matching is intentionally forgiving: any of these all map
to "HB 195":

    HB195.pdf
    HB 195.pdf
    hb195.pdf
    Blue Sheet - HB195 5.20.26.pdf
    HB195CS(STA)-DCCED-CBPL-BS-05-12-26.pdf
    2026 Blue Sheet HB 195.docx
    CSHB195-DOH-DPH-5-18-26.pdf

The matcher pulls the FIRST occurrence of any bill prefix
(HB/SB/HJR/SJR/HCR/SCR/HR/SR/HSCR/SSCR) followed by digits,
optionally preceded by a "CS/HCS/SCS" committee-substitute marker.
"""

import os
import re
import time

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blue_sheets")

# Permissible bill-number prefixes, longest first so e.g. "HSCR"
# matches before "HCR" or "SR".
_PREFIXES = ("HSCR", "SSCR", "HJR", "SJR", "HCR", "SCR", "HB", "SB", "HR", "SR")
_PREFIX_PATTERN = "|".join(_PREFIXES)

# First occurrence of [optional CS marker] + prefix + zero-padded digits
# inside any filename. Captures (prefix, number).
_BILL_RE = re.compile(
    r"(?:CS|HCS|SCS|SCS\s?CS|HCS\s?CS)?"  # optional committee-substitute
    r"(" + _PREFIX_PATTERN + r")"
    r"\s*0*(\d+)",
    re.IGNORECASE,
)

# Accept PDFs (preferred — browsers render inline) and DOCX/DOC
# (browsers download — still functional if that's what the user has).
_EXT_RE = re.compile(r"\.(pdf|docx?)$", re.IGNORECASE)

# In-memory cache so we don't re-scan disk on every request.
_cache_value = None
_cache_time = 0.0
_CACHE_TTL = 60.0


def _extract_billnumber(filename):
    """Pull the canonical billnumber out of a filename, or None."""
    if not _EXT_RE.search(filename):
        return None
    m = _BILL_RE.search(filename)
    if not m:
        return None
    prefix = m.group(1).upper()
    number = str(int(m.group(2)))  # strip leading zeros
    return f"{prefix} {number}"


def _scan():
    """Return {canonical_billnumber: filename_on_disk}.
    If multiple files map to the same bill, keep the first by name —
    deterministic but arbitrary. User can de-dupe by removing extras."""
    out = {}
    if not os.path.isdir(_DIR):
        return out
    for fn in sorted(os.listdir(_DIR)):
        bn = _extract_billnumber(fn)
        if not bn:
            continue
        if bn not in out:
            out[bn] = fn
    return out


def index():
    """Cached {billnumber: filename} mapping."""
    global _cache_value, _cache_time
    now = time.monotonic()
    if _cache_value is not None and (now - _cache_time) < _CACHE_TTL:
        return _cache_value
    _cache_value = _scan()
    _cache_time = now
    return _cache_value


def filename_for(billnumber):
    """Return the on-disk filename for a billnumber, or None."""
    return index().get((billnumber or "").strip())


def abs_path(filename):
    """Resolve a sanitized filename to an absolute path inside the
    blue_sheets/ dir. Returns None if the filename would escape the
    directory (defensive — prevents path traversal)."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    candidate = os.path.normpath(os.path.join(_DIR, filename))
    # Must be a regular file directly inside _DIR
    if not candidate.startswith(_DIR + os.sep):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate
