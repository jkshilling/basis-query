"""Per-bill workflow flags, persisted on disk.

Three states cycle on each click of the card's flag button:
    "" (default)  →  "needs-research"  →  "reviewed"  →  "decided"  →  ""

The flags persist in flags.json (next to the other persistence files
like .bill_summaries_cache.json). One file mapping billnumber →
{state, updated_at}. Atomic write via tmpfile + rename.

Public API:
    get_flags()             # {billnumber: {state, updated_at}}
    get_flag(bn)            # one bill's state ("" if unset)
    set_flag(bn, state)     # write specific state ("" clears)
    cycle_flag(bn)          # advance to next state in cycle
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("basis_browser.flags")

# Allowed states in cycle order. The cycle wraps back to "" at the end.
STATES = ("", "needs-research", "reviewed", "decided")
_NEXT_STATE = {STATES[i]: STATES[(i + 1) % len(STATES)] for i in range(len(STATES))}

_FLAGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "flags.json",
)
_LOCK = threading.Lock()
_FLAGS: dict | None = None


def _load() -> dict:
    global _FLAGS
    if _FLAGS is not None:
        return _FLAGS
    with _LOCK:
        if _FLAGS is not None:
            return _FLAGS
        try:
            with open(_FLAGS_PATH, "r") as f:
                _FLAGS = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _FLAGS = {}
    return _FLAGS


def _save():
    flags = _load()
    with _LOCK:
        tmp = _FLAGS_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(flags, f, indent=2, sort_keys=True)
            os.replace(tmp, _FLAGS_PATH)
        except OSError as e:
            log.warning("flags.save_failed err=%r", e)


def get_flags() -> dict:
    """Return the full flag map. Mutating the returned dict is safe
    but won't persist unless you call _save() yourself."""
    return dict(_load())


def get_flag(billnumber: str) -> str:
    """Return one bill's current state ('' if unset or unknown bn)."""
    return (_load().get((billnumber or "").strip()) or {}).get("state", "")


def set_flag(billnumber: str, state: str) -> str:
    """Set a specific state. Pass '' to clear. Returns the new state.
    Unknown state strings raise ValueError."""
    bn = (billnumber or "").strip()
    if not bn:
        raise ValueError("billnumber required")
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}; valid: {STATES}")
    flags = _load()
    if state:
        flags[bn] = {"state": state, "updated_at": int(time.time())}
    else:
        flags.pop(bn, None)
    _save()
    return state


def cycle_flag(billnumber: str) -> str:
    """Advance one bill's state to the next in the cycle. Returns the
    new state. Untouched bills enter the cycle at 'needs-research'."""
    current = get_flag(billnumber)
    nxt = _NEXT_STATE.get(current, "needs-research")
    return set_flag(billnumber, nxt)
