"""Simple time-based in-memory cache."""

import time


_cache = {}


def get(key, max_age=300):
    """Return cached value if it exists and is younger than max_age seconds."""
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry["time"]) < max_age:
        return entry["value"]
    return None


def put(key, value):
    _cache[key] = {"value": value, "time": time.monotonic()}
