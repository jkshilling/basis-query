"""Blue-sheet index: scan basis-browser-app/blue_sheets/ for PDFs
and map each filename back to a canonical billnumber.

A "blue sheet" is a one-page legislative analysis attached to a
bill — the term comes from the (historically blue) paper they were
printed on. Drop a PDF in the folder with the bill number as the
filename and the Governor's Desk page picks it up automatically.
"""

import os
import re
import time

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blue_sheets")

# Filename → canonical billnumber. e.g. "HB195.pdf" → "HB 195",
# "HJR  4.pdf" → "HJR 4". The mapping rule mirrors what BASIS uses.
_FILENAME_RE = re.compile(
    r"^([A-Za-z]+)\s*0*(\d+)\.pdf$",
    re.IGNORECASE,
)

# In-memory cache so we don't re-scan the disk on every request.
# 60-second TTL = good middle ground: adding a new blue sheet
# appears on the page within a minute without an app restart.
_cache_value = None
_cache_time = 0.0
_CACHE_TTL = 60.0


def _scan():
    """Return {canonical_billnumber: filename_on_disk}."""
    out = {}
    if not os.path.isdir(_DIR):
        return out
    for fn in os.listdir(_DIR):
        if not fn.lower().endswith(".pdf"):
            continue
        m = _FILENAME_RE.match(fn)
        if not m:
            continue
        prefix = m.group(1).upper()
        number = str(int(m.group(2)))  # strip leading zeros
        out[f"{prefix} {number}"] = fn
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
    return index().get(billnumber.strip())


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
