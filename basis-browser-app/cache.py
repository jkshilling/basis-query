"""Time-based cache with persistent disk backing and bounded size.

Values are stored in memory for fast access and mirrored to a JSON file
so the cache survives process restarts. Past hearing windows and other
historical data persist for days/weeks; restarts no longer trigger a
~60-second cold load every time.

Bounded size: at most MAX_ENTRIES total entries; oldest are evicted first.
Age-based purge at startup drops anything older than MAX_AGE_SECONDS.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("basis_browser.cache")

# Cache file location. Override with BASIS_CACHE_FILE env var.
_DEFAULT_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".cache.json",
)
_CACHE_FILE = os.environ.get("BASIS_CACHE_FILE", _DEFAULT_CACHE_FILE)

# Bounds.
MAX_ENTRIES = int(os.environ.get("BASIS_CACHE_MAX_ENTRIES", "1000"))
MAX_AGE_SECONDS = int(os.environ.get("BASIS_CACHE_MAX_AGE", str(60 * 24 * 3600)))  # 60 days

_cache: dict = {}
_lock = threading.Lock()
_save_pending = False
_save_timer: threading.Timer | None = None


def _load_from_disk() -> None:
    """Load cache from disk on startup, dropping entries older than MAX_AGE_SECONDS."""
    global _cache
    if not os.path.exists(_CACHE_FILE):
        return
    try:
        with open(_CACHE_FILE, "r") as f:
            raw = json.load(f)
        now_wall = time.time()
        now_mono = time.monotonic()
        loaded = 0
        purged = 0
        for key, entry in raw.items():
            wall_age = now_wall - entry.get("wall_time", now_wall)
            if wall_age > MAX_AGE_SECONDS:
                purged += 1
                continue
            _cache[key] = {
                "value": entry["value"],
                "time": now_mono - wall_age,
            }
            loaded += 1
        log.info("cache.load loaded=%d purged=%d file=%s", loaded, purged, _CACHE_FILE)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        log.warning("cache.load_failed err=%s", exc)
        _cache = {}


def _enforce_max_entries() -> None:
    """If cache exceeds MAX_ENTRIES, drop oldest first."""
    if len(_cache) <= MAX_ENTRIES:
        return
    # Sort by time ascending (oldest first), drop the excess.
    items = sorted(_cache.items(), key=lambda kv: kv[1]["time"])
    excess = len(_cache) - MAX_ENTRIES
    for key, _ in items[:excess]:
        _cache.pop(key, None)
    log.info("cache.evict count=%d remaining=%d", excess, len(_cache))


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
        log.debug("cache.save entries=%d", len(snapshot))
    except (OSError, TypeError) as exc:
        log.warning("cache.save_failed err=%s", exc)


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
    _enforce_max_entries()
    _schedule_save()


def flush() -> None:
    """Force an immediate disk save (e.g. on shutdown)."""
    _save_to_disk_now()


# Load existing cache on import.
_load_from_disk()
