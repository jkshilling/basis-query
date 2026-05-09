"""Backwards-compatible facade.

The implementation has been split into three focused modules:

  parse.py    — pure data transformations (no I/O)
  fetch.py    — BASIS API calls and akleg HTML scraping
  metrics.py  — derived data (dashboard, governor, bill progress, etc.)

New code should import from those directly. This file re-exports the
public names that other parts of the app (notably app.py) used to import
from `adapter`.
"""

from __future__ import annotations

# --- Pure helpers (formerly _-prefixed in adapter.py) -----------------------
from parse import (
    parse_bills,
    parse_bills_extended as _parse_bills_extended,
    strip_ns as _strip_ns,
    child_text as _child_text,
    child_attr as _child_attr,
    format_status_date as _format_status_date,
    compact_billnumber as _compact_billnumber,
    current_chamber as _current_chamber,
    truncate as _truncate,
    compact_hearing as _compact_hearing,
    parse_hearing_datetime as _parse_hearing_datetime,
    next_referral as _next_referral,
)

# --- I/O wrappers ----------------------------------------------------------
from fetch import (
    fetch as _fetch,
    fetch_all_bills as _fetch_all_bills,
    fetch_hearing_schedule as _fetch_hearing_schedule,
    fetch_hearing_window as _fetch_hearing_window,
    fetch_hearing_counts as _fetch_hearing_counts,
    fetch_committee_reports as _fetch_committee_reports,
    scan_all_actions as _scan_all_actions,
    count_actions_by_year as _count_actions_by_year,
    fetch_bill_detail as _fetch_bill_detail,
    SCHEDULE_URL,
    ACTION_LABELS as _ACTION_LABELS,
)

# --- Derived data ----------------------------------------------------------
from metrics import (
    house_bills_in_senate,
    senate_bills_in_house,
    dashboard_stats,
    action_code_counts,
    bill_progress,
    governor_bills,
    activity_feed,
    committee_detail,
    top_subjects,
    search_bills,
    cache_freshness,
    NOTABLE_CODES as _NOTABLE_CODES,
)
