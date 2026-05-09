"""Time-based cache with persistent disk backing.

Values are stored in memory for fast access and mirrored to a JSON file
so the cache survives process restarts. Past hearing windows and other
historical data persist for days/weeks; restarts no longer trigger a
~60-second cold load every time.
"""

from __future__ import annotations

import json
import os
import threading
import time

# Cache file location. Override with BASIS_CACHE_FILE env var.
_DEFAULT_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".cache.json",
)
_CACHE_FILE = os.environ.get("BASIS_CACHE_FILE", _DEFAULT_CACHE_FILE)

_cache: dict = {}
_lock = threading.Lock()
_save_pending = False
_save_timer: threading.Timer | None = None


def _load_from_disk() -> None:
    """Load cache from disk on startup."""
    global _cache
    if not os.path.exists(_CACHE_FILE):
        return
    try:
        with open(_CACHE_FILE, "r") as f:
            raw = json.load(f)
        # On disk we store wall-clock timestamps; in-memory uses monotonic.
        # We adjust by recording each entry's wall-clock age and converting.
        now_wall = time.time()
        now_mono = time.monotonic()
        for key, entry in raw.items():
            wall_age = now_wall - entry.get("wall_time", now_wall)
            _cache[key] = {
                "value": entry["value"],
                "time": now_mono - wall_age,  # back-date in monotonic terms
            }
    except (OSError, json.JSONDecodeError, KeyError):
        # Corrupt cache file — start fresh.
        _cache = {}


def _save_to_disk_now() -> None:
    """Atomically write the current cache to disk."""
    global _save_pending
    with _lock:
        _save_pending = False
        # Convert monotonic times back to wall-clock for persistence.
        now_wall = time.time()
        now_mono = time.monotonic()
        snapshot = {}
        for key, entry in _cache.items():
            age = now_mono - entry["time"]
            snapshot[key] = {
                "value": entry["value"],
                "wall_time": now_wall - age,
            }
    try:
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, default=str)
        os.replace(tmp, _CACHE_FILE)
    except (OSError, TypeError):
        # If we can't serialize a value, skip the save rather than crash.
        pass


def _schedule_save(delay: float = 5.0) -> None:
    """Debounce disk writes so a burst of put() calls only saves once."""
    global _save_pending, _save_timer
    with _lock:
        if _save_pending:
            return
        _save_pending = True
        if _save_timer is not None:
            _save_timer.cancel()
        _save_timer = threading.Timer(delay, _save_to_disk_now)
        _save_timer.daemon = True
        _save_timer.start()


def get(key, max_age=300):
    """Return cached value if it exists and is younger than max_age seconds."""
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry["time"]) < max_age:
        return entry["value"]
    return None


def put(key, value):
    _cache[key] = {"value": value, "time": time.monotonic()}
    _schedule_save()


def flush() -> None:
    """Force an immediate disk save (e.g. on shutdown)."""
    _save_to_disk_now()


# Load existing cache on import.
_load_from_disk()
