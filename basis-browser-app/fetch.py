"""I/O layer: BASIS API calls, akleg HTML scraping, and the cached
paginated scans built on top.

Imports parse.py for XML parsing and string helpers. No imports from
metrics.py.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import cache as _cache
from parse import (
    strip_ns, child_text, child_attr,
    parse_bills, parse_bills_extended, parse_hearing_datetime,
    compact_billnumber, format_status_date, truncate,
)

# Make repo root importable so we can call query_basis.fetch_basis().
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import query_basis  # noqa: E402

log = logging.getLogger("basis_browser.fetch")

SCHEDULE_URL = "https://www.akleg.gov/basis/Meeting/Index"


# --- Low-level BASIS API call ---

def fetch(section, session="34", chamber=None, queries=None, result_range=None):
    """Thin wrapper around query_basis.fetch_basis()."""
    return query_basis.fetch_basis(
        base_url=query_basis.DEFAULT_BASE_URL,
        section=section,
        session=session,
        chamber=chamber,
        queries=queries or [],
        result_range=result_range,
        version=query_basis.DEFAULT_VERSION,
    )


# --- Paginated, cached bill list ---

def fetch_all_bills(chamber, session="34", queries=None):
    """Fetch all bills for a chamber with optional expansions. Cached
    for 10 minutes."""
    cache_key = f"all_bills_{session}_{chamber}_{','.join(queries or [])}"
    cached = _cache.get(cache_key, max_age=600)
    if cached is not None:
        return cached

    PAGE_SIZE = 100
    all_bills = []
    start = 1
    while True:
        end = start + PAGE_SIZE - 1
        range_str = f"{start}..{end}" if start > 1 else f"..{PAGE_SIZE}"
        result = fetch(
            section="bills", session=session,
            chamber=chamber, queries=queries,
            result_range=range_str,
        )
        page = parse_bills_extended(result) if queries else parse_bills(result)
        if not page:
            break
        all_bills.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    seen = set()
    unique = []
    for b in all_bills:
        if b["billnumber"] not in seen:
            seen.add(b["billnumber"])
            unique.append(b)

    _cache.put(cache_key, unique)
    return unique


# --- Cached scan of all bills with Actions expansion ---

def scan_all_actions(session="34"):
    """Fetch all bills with Actions and return raw action data.

    Returns list of (billnumber, chamber, short_title, status, actions_list)
    where actions_list is [(code, chamber, journaldate, action_text), ...].
    """
    cached = _cache.get("all_actions")
    if cached is not None:
        return cached

    bills = []
    for chamber in ["H", "S"]:
        seen = set()
        start = 1
        while True:
            end = start + 99
            range_str = f"{start}..{end}" if start > 1 else "..100"
            result = fetch(
                section="bills", session=session, chamber=chamber,
                queries=["Actions"], result_range=range_str,
            )
            body = result["body"].decode("utf-8", errors="replace")
            if len(body) < 300 or "FaultException" in body:
                break
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                break
            page_count = 0
            for bill in root.iter():
                if strip_ns(bill.tag) != "Bill":
                    continue
                bn = bill.attrib.get("billnumber", "").strip()
                if bn in seen:
                    continue
                seen.add(bn)
                page_count += 1
                short_title = child_text(bill, "ShortTitle")
                status = child_text(bill, "StatusText")
                bill_actions = []
                for actions_elem in bill:
                    if strip_ns(actions_elem.tag) != "Actions":
                        continue
                    for action in actions_elem:
                        bill_actions.append((
                            action.attrib.get("code", ""),
                            action.attrib.get("chamber", ""),
                            action.attrib.get("journaldate", ""),
                            child_text(action, "ActionText"),
                        ))
                bills.append((
                    compact_billnumber(bn),
                    chamber,
                    truncate(short_title),
                    status,
                    bill_actions,
                ))
            if page_count < 100:
                break
            start += 100

    _cache.put("all_actions", bills)
    return bills


# --- Hearing schedule scraping ---

def fetch_hearing_schedule(chamber="S", days=7):
    """Scrape the akleg hearing schedule HTML to build a map of
    bill_number -> next hearing datetime string. Only includes hearings
    that haven't happened yet in AKDT.
    """
    try:
        from zoneinfo import ZoneInfo
        now_ak = datetime.now(ZoneInfo("America/Anchorage")).replace(tzinfo=None)
    except ImportError:
        now_ak = datetime.now()

    today = now_ak
    start = today.strftime("%-m/%-d/%Y")
    end = (today + timedelta(days=days)).strftime("%-m/%-d/%Y")

    url = (
        f"{SCHEDULE_URL}?mode=results&type=&com=&"
        f"startDate={start}&endDate={end}&chamber={chamber}"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "basis-browser/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return {}

    bill_hearings = {}
    current_datetime = ""
    for line in html.split("\n"):
        date_match = re.search(
            r'<td colspan="2">((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+\s+\w+\s+\d+:\d+\s+[AP]M)</td>',
            line,
        )
        if date_match:
            current_datetime = date_match.group(1)
            continue

        bill_match = re.search(
            r'Bill/Detail/\?Root=([^"]+)"[^>]*>([^<]+)</a>',
            line,
        )
        if bill_match and current_datetime:
            hearing_dt = parse_hearing_datetime(current_datetime, year=now_ak.year)
            if hearing_dt and hearing_dt < now_ak:
                continue
            bill_num = bill_match.group(2).strip()
            if bill_num not in bill_hearings:
                bill_hearings[bill_num] = current_datetime

    return bill_hearings


def fetch_hearing_window(session, start_date, end_date, cache_for_seconds):
    """Fetch hearings for a single 2-week window. Cached per-window."""
    from collections import Counter

    s = start_date.strftime("%m/%d/%y")
    e = end_date.strftime("%m/%d/%y")
    key = f"hearings_{session}_{s}_{e}"

    cached = _cache.get(key, max_age=cache_for_seconds)
    if cached is not None:
        return Counter(cached)

    counts = Counter()
    try:
        result = fetch(
            section="meetings", session=session,
            queries=[f"Meetings;startdate={s};enddate={e}"],
            result_range="..50",
        )
        body = result["body"].decode("utf-8", errors="replace")
        if len(body) > 300 and "FaultException" not in body:
            root = ET.fromstring(body)
            for elem in root.iter():
                if strip_ns(elem.tag) != "Meeting":
                    continue
                if elem.attrib.get("Canceled") == "true":
                    continue
                ch = child_text(elem, "chamber")
                sponsor = child_text(elem, "Sponsor")
                if ch and sponsor:
                    counts[f"({ch}) {sponsor}"] += 1
    except Exception:
        pass

    _cache.put(key, dict(counts))
    return counts


def fetch_hearing_counts(session="34"):
    """Aggregate hearings per committee across the full session using
    parallel 2-week window fetches. Each window is cached per its age:
    >14 days old → 30 days; recent → 1 hour."""
    from collections import Counter
    import datetime as dt

    hearings = Counter()
    today = dt.date.today()
    start = dt.date(2025, 1, 1)
    end_date = today

    windows = []
    cursor = start
    while cursor < end_date:
        window_end = min(cursor + dt.timedelta(days=13), end_date)
        windows.append((cursor, window_end))
        cursor = window_end + dt.timedelta(days=1)

    def _one(args):
        ws, we = args
        days_old = (today - we).days
        cache_seconds = 30 * 24 * 3600 if days_old > 14 else 3600
        return fetch_hearing_window(session, ws, we, cache_seconds)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for window_counts in ex.map(_one, windows):
            hearings.update(window_counts)

    return hearings


def fetch_committee_reports(session="34"):
    """Count bills reported out by each committee (action code 002).
    Reuses scan_all_actions cache — no extra API calls."""
    from collections import Counter

    reported = Counter()
    for bn, origin, title, status, actions in scan_all_actions(session):
        for code, achamber, jdate, text in actions:
            if code == "002":
                cmte = text.split(" ")[0] if text else ""
                if cmte and achamber:
                    reported[f"({achamber}) {cmte}"] += 1
    return reported


# --- Action code counting (used by /action-codes) ---

def count_actions_by_year(session, years):
    """Count action codes by year for a given session. Uses live API
    pagination (not the scan_all_actions cache, since this needs
    journaldate-level filtering)."""
    from collections import Counter
    counts = {y: Counter() for y in years}

    for chamber in ["H", "S"]:
        start = 1
        while True:
            end = start + 99
            range_str = f"{start}..{end}" if start > 1 else "..100"
            result = fetch(
                section="bills", session=session, chamber=chamber,
                queries=["Actions"], result_range=range_str,
            )
            body = result["body"].decode("utf-8", errors="replace")
            if len(body) < 300 or "FaultException" in body:
                break
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                break

            page_count = 0
            for bill in root.iter():
                if strip_ns(bill.tag) != "Bill":
                    continue
                page_count += 1
                for actions in bill:
                    if strip_ns(actions.tag) != "Actions":
                        continue
                    for action in actions:
                        code = action.attrib.get("code", "")
                        jdate = action.attrib.get("journaldate", "")
                        for y in years:
                            if jdate.startswith(y):
                                counts[y][code] += 1

            if page_count < 100:
                break
            start += 100

    return {y: dict(c) for y, c in counts.items()}


# --- Single-bill detail fetch ---

def fetch_bill_detail(billnumber, session="34"):
    """Fetch one bill with all expansions (Actions/Sponsors/Versions/
    Subjects) for the detail page. Cached for 10 minutes."""
    key = f"bill_detail_{session}_{billnumber}"
    cached = _cache.get(key, max_age=600)
    if cached is not None:
        return cached

    prefix = billnumber.strip().split()[0] if billnumber else ""
    chamber = "H" if prefix.startswith("H") else "S"

    queries = [
        f"Bills;billnumber={billnumber}",
        "Actions", "Sponsors", "Versions", "Subjects",
    ]
    result = fetch(
        section="bills", session=session, chamber=chamber,
        queries=queries, result_range="..1",
    )
    body = result["body"].decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None

    bill_elem = None
    for elem in root.iter():
        if strip_ns(elem.tag) == "Bill":
            if elem.attrib.get("billnumber", "").strip() == billnumber.strip() or \
               compact_billnumber(elem.attrib.get("billnumber", "")) == billnumber.strip():
                bill_elem = elem
                break
    if bill_elem is None:
        # Filter sometimes returns wrong bill — fall back to scanning.
        for rng in ["..100", "101..200", "201..300", "301..400", "401..500"]:
            rr = fetch(
                section="bills", session=session, chamber=chamber,
                queries=["Actions", "Sponsors", "Versions", "Subjects"],
                result_range=rng,
            )
            b = rr["body"].decode("utf-8", errors="replace")
            if len(b) < 300 or "FaultException" in b:
                break
            r2 = ET.fromstring(b)
            for elem in r2.iter():
                if strip_ns(elem.tag) != "Bill":
                    continue
                if compact_billnumber(elem.attrib.get("billnumber", "")) == billnumber.strip():
                    bill_elem = elem
                    break
            if bill_elem is not None:
                break

    if bill_elem is None:
        return None

    detail = _extract_bill_detail(bill_elem)
    _cache.put(key, detail)
    return detail


# Map of action codes to short labels for the bill detail timeline.
ACTION_LABELS = {
    "001": "First Reading",
    "002": "Committee Report",
    "003": "Referred",
    "004": "Referral Changed",
    "005": "Cmte Report Received",
    "006": "Referral Replaced",
    "008": "Second Reading",
    "009": "CS Adopted (UC)",
    "011": "Amendment Adopted (UC)",
    "012": "Amendment Adopted",
    "013": "Amendment Failed",
    "014": "Held in 2nd Reading",
    "015": "Advanced to 3rd",
    "016": "Third Reading",
    "017": "Returned to 2nd",
    "018": "Held in 3rd Reading",
    "020": "Floor Vote",
    "021": "Effective Date",
    "022": "Transmitted to Other Chamber",
    "026": "Concur Amendment",
    "027": "Failed to Concur",
    "029": "Conference Cmte Appointed",
    "030": "Conference Cmte Members",
    "032": "Conference Report Adopted",
    "033": "Transmitted to Governor",
    "034": "Signed Into Law",
    "036": "Law Without Signature",
    "037": "Permanently Filed",
    "038": "Vetoed",
    "039": "Veto Sustained",
    "040": "Veto Overridden",
    "041": "Effective Date of Law",
    "043": "Manifest Error",
    "048": "Title Change",
    "050": "Governor's Letter",
    "052": "Sponsor Substitute",
    "053": "Withdrawn",
    "057": "Amendment to Amendment",
    "060": "Failed Passage",
    "062": "Effective Date Adopted",
    "076": "Held to Calendar",
    "080": "Rules to Calendar",
    "083": "Transmitted as Amended",
    "091": "Referral List",
    "092": "Cosponsor Change",
    "094": "Title Change",
    "095": "Hearing Notice Waived",
    "100": "Cross Sponsor",
    "103": "Version",
    "105": "Fiscal Note",
    "121": "Prefile Released",
    "122": "CS Adopted",
    "129": "Chapter Number",
    "130": "Amendment Offered",
    "131": "Amendment Not Offered",
    "132": "Amendment Tabled",
}


def _extract_bill_detail(bill_elem):
    """Pull a single Bill element's full structured detail."""
    bn = compact_billnumber(bill_elem.attrib.get("billnumber", ""))
    detail = {
        "billnumber": bn,
        "chamber": bill_elem.attrib.get("chamber", "").strip(),
        "short_title": child_text(bill_elem, "ShortTitle"),
        "status": child_text(bill_elem, "StatusText"),
        "status_date": format_status_date(child_text(bill_elem, "StatusDate")),
        "committee": child_text(bill_elem, "CurrentCommittee"),
        "committee_code": child_attr(bill_elem, "CurrentCommittee", "committeecode"),
        "sponsors": [],
        "cosponsors": [],
        "committee_sponsor": "",
        "subjects": [],
        "versions": [],
        "actions": [],
        "fiscal_notes": [],
    }

    for sponsors in bill_elem:
        if strip_ns(sponsors.tag) != "Sponsors":
            continue
        for member in sponsors:
            if strip_ns(member.tag) == "MemberDetails":
                first = child_text(member, "FirstName")
                last = child_text(member, "LastName")
                rec = {
                    "name": f"{first} {last}".strip(),
                    "party": child_text(member, "Party"),
                    "district": child_text(member, "District"),
                }
                if member.attrib.get("primesponsor") == "true":
                    detail["sponsors"].append(rec)
                else:
                    detail["cosponsors"].append(rec)
            elif strip_ns(member.tag) == "Committee":
                detail["committee_sponsor"] = member.attrib.get("code", "")

    for subs in bill_elem:
        if strip_ns(subs.tag) != "Subjects":
            continue
        for sub in subs:
            if strip_ns(sub.tag) == "Subject":
                txt = (sub.text or "").strip()
                if txt:
                    detail["subjects"].append(txt)

    for vers in bill_elem:
        if strip_ns(vers.tag) != "Versions":
            continue
        for v in vers:
            if strip_ns(v.tag) != "Version":
                continue
            detail["versions"].append({
                "letter": v.attrib.get("versionletter", ""),
                "name": v.attrib.get("name", ""),
                "intro_date": format_status_date(v.attrib.get("introdate", "")),
                "title": child_text(v, "Title"),
            })

    for acts in bill_elem:
        if strip_ns(acts.tag) != "Actions":
            continue
        for action in acts:
            if strip_ns(action.tag) != "Action":
                continue
            code = action.attrib.get("code", "")
            text = child_text(action, "ActionText")
            jdate = action.attrib.get("journaldate", "")
            achamber = action.attrib.get("chamber", "")
            label = ACTION_LABELS.get(code, "Action")
            detail["actions"].append({
                "code": code,
                "label": label,
                "chamber": achamber,
                "date": format_status_date(jdate),
                "raw_date": jdate,
                "text": text,
            })
            if code in ("105", "106", "107", "108"):
                detail["fiscal_notes"].append({
                    "date": format_status_date(jdate),
                    "text": text,
                })

    detail["actions"].sort(key=lambda a: a["raw_date"], reverse=True)
    return detail
