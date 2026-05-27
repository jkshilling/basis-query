"""Per-bill workflow flags, persisted on disk.

Three independent flag dimensions per bill, each with its own cycle:

  workflow:  ""  →  needs-research  →  reviewed  →  decided  →  ""
  gov_pref:  ""  →  SIGN  →  VETO  →  LWOS  →  UNDECIDED  →  ""
  followup:  ""  →  need-blue-sheet  →  need-legal  →  need-political
                 →  need-fiscal  →  need-brief-gov  →  ""

Flags persist in flags.json. Schema (legacy entries with a top-level
'state' key are auto-migrated on first read):

  {
    "HB 110": {
      "workflow": "needs-research",
      "gov_pref": "SIGN",
      "followup": "need-blue-sheet",
      "updated_at": 1234567890
    }
  }

Public API:
    get_flags()                          # {billnumber: dimensions dict}
    get_flag(bn, dimension)              # one dimension's state ("" if unset)
    set_flag(bn, dimension, state)       # write specific state ("" clears)
    cycle_flag(bn, dimension)            # advance to next state in that cycle
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("basis_browser.flags")

# Cycle definitions per dimension. Empty string = default / unset.
_CYCLES = {
    "workflow": ("", "needs-research", "reviewed", "decided"),
    "gov_pref": ("", "SIGN", "VETO", "LWOS", "UNDECIDED"),
    "followup": ("",
                 "need-blue-sheet",
                 "need-legal",
                 "need-political",
                 "need-fiscal",
                 "need-brief-gov"),
}

DIMENSIONS = tuple(_CYCLES.keys())

_NEXT = {
    dim: {states[i]: states[(i + 1) % len(states)] for i in range(len(states))}
    for dim, states in _CYCLES.items()
}

# Default-entry state for each dimension (the first non-empty in cycle).
_FIRST_ACTIVE = {dim: states[1] for dim, states in _CYCLES.items()}

_FLAGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "flags.json",
)
_LOCK = threading.Lock()
_FLAGS: dict | None = None


def _migrate_legacy(entry):
    """Old schema stored {state, updated_at} for a single dimension.
    New schema stores {workflow, gov_pref, followup, updated_at}. Map
    any legacy entry forward so we never lose data."""
    if not isinstance(entry, dict):
        return {}
    if "state" in entry and "workflow" not in entry:
        # Single-dimension legacy entry.
        return {
            "workflow": entry.get("state", ""),
            "gov_pref": "",
            "followup": "",
            "updated_at": entry.get("updated_at", int(time.time())),
        }
    # Already new shape — fill in missing dimensions.
    return {
        "workflow":   entry.get("workflow", ""),
        "gov_pref":   entry.get("gov_pref", ""),
        "followup":   entry.get("followup", ""),
        "updated_at": entry.get("updated_at", int(time.time())),
    }


def _load() -> dict:
    global _FLAGS
    if _FLAGS is not None:
        return _FLAGS
    with _LOCK:
        if _FLAGS is not None:
            return _FLAGS
        try:
            with open(_FLAGS_PATH, "r") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        # Migrate every entry forward on load. Idempotent.
        _FLAGS = {bn: _migrate_legacy(v) for bn, v in (raw or {}).items()}
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


def _is_all_blank(entry):
    return not any(entry.get(dim) for dim in DIMENSIONS)


def get_flags() -> dict:
    """Return the full flag map. {billnumber: {workflow, gov_pref,
    followup, updated_at}}."""
    return dict(_load())


def get_flag(billnumber: str, dimension: str) -> str:
    """Return one dimension's current state for a bill. '' if unset
    or if the dimension/bn is unknown."""
    if dimension not in DIMENSIONS:
        return ""
    return (_load().get((billnumber or "").strip()) or {}).get(dimension, "") or ""


def set_flag(billnumber: str, dimension: str, state: str) -> str:
    """Set one dimension to a specific state. '' clears it. Returns
    the new state. Unknown dimension or state raises ValueError."""
    bn = (billnumber or "").strip()
    if not bn:
        raise ValueError("billnumber required")
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; valid: {DIMENSIONS}")
    valid = _CYCLES[dimension]
    if state not in valid:
        raise ValueError(f"unknown state {state!r} for {dimension!r}; valid: {valid}")
    flags = _load()
    entry = flags.get(bn) or {dim: "" for dim in DIMENSIONS}
    entry[dimension] = state
    entry["updated_at"] = int(time.time())
    if _is_all_blank(entry):
        flags.pop(bn, None)
    else:
        flags[bn] = entry
    _save()
    return state


def cycle_flag(billnumber: str, dimension: str) -> str:
    """Advance one dimension to the next state in its cycle."""
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}; valid: {DIMENSIONS}")
    current = get_flag(billnumber, dimension)
    nxt = _NEXT[dimension].get(current, _FIRST_ACTIVE[dimension])
    return set_flag(billnumber, dimension, nxt)
