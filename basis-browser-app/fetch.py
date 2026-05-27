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
import html as _html
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
FLOOR_URL = "https://www.akleg.gov/basis/floor.asp"


def is_procedural_resolution(billnumber, title):
    """True if the bill is a procedural HCR/SCR that only suspends or
    amends the chambers' Uniform Rules — pure plumbing, not policy.

    Examples filtered: "SUSPEND UNIFORM RULES FOR SB 64",
    "AMEND UNIFORM RULES: ABSTAIN FROM VOTING",
    "UNIFORM RULES: COMMITTEE RECORDS".
    """
    bn = (billnumber or "").upper()
    if not (bn.startswith("HCR") or bn.startswith("SCR")):
        return False
    t = (title or "").upper()
    return "UNIFORM RULES" in t or "UNIFORM RULE " in t


def fetch_floor_calendar(chamber, date=None):
    """Scrape the akleg floor calendar HTML for one chamber on one date.

    Returns a list of dicts:
      [{billnumber, title, status, section, live}]
    where:
      section : '' for the main calendar; otherwise a header like
                'HOUSE LEGISLATION AWAITING RECEDE IN SENATE AMENDMENTS'
      live    : True when the legislature marks the bill as currently
                being processed (akleg wraps the title in red text)
    """
    if date is None:
        try:
            from zoneinfo import ZoneInfo
            now_ak = datetime.now(ZoneInfo("America/Anchorage"))
        except ImportError:
            now_ak = datetime.now()
        date = now_ak.strftime("%-m/%-d/%Y")

    url = f"{FLOOR_URL}?date={date}&chamber={chamber}"
    cache_key = f"floor_cal_{chamber}_{date}"
    # Short cache so the LIVE badge tracks the legislature's session in
    # near-real-time. Auto-refresh on the dashboard polls every 60s.
    cached = _cache.get(cache_key, max_age=45)
    if cached is not None:
        return cached

    bills = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "basis-browser/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("floor.fetch_failed chamber=%s date=%s err=%r", chamber, date, exc)
        return bills

    # Each <li> is either a bill row (col01..col04) or a section header
    # (bold centered div). Split on <li> boundaries and inspect each.
    li_blocks = re.split(r'<li[^>]*>', html)
    current_section = ""
    for block in li_blocks:
        # Section header — bold centered text inside a div
        header_match = re.search(
            r'font-weight:bold;text-align:center[^>]*>\s*([^<]+?)\s*</div>',
            block,
        )
        if header_match:
            txt = header_match.group(1).strip()
            # Ignore generic chamber headers like "SENATE"
            if txt and txt.upper() not in ("HOUSE", "SENATE") and "CALENDAR" not in txt.upper():
                current_section = txt
            continue

        # Bill link
        bm = re.search(r'Bill/Detail/\d*\?Root=([^"]+)"[^>]*>([^<]+)</a>', block)
        if not bm:
            continue
        bn = " ".join(bm.group(2).strip().split())

        # Title in col02. Look for <font color=red> to detect live flag.
        title = ""
        live = False
        tm = re.search(
            r'class="col02">\s*(?:<font\s+color=red>)?\s*([^<]+?)(?:</font>)?\s*</span>',
            block,
        )
        if tm:
            title = tm.group(1).strip()
        if re.search(r'class="col02">[^<]*<font\s+color=red>', block):
            live = True

        # Status in col03
        status = ""
        sm = re.search(r'class="col03">\s*([^<]+?)\s*</span>', block)
        if sm:
            status = sm.group(1).strip()

        bills.append({
            "billnumber": bn,
            "title": title,
            "status": status,
            "section": current_section,
            "live": live,
        })

    # Deduplicate: a bill appearing under multiple sections keeps its
    # first occurrence (the main calendar entry, which is listed first).
    seen = set()
    unique = []
    for b in bills:
        if b["billnumber"] in seen:
            continue
        seen.add(b["billnumber"])
        unique.append(b)

    _cache.put(cache_key, unique)
    return unique


# --- Low-level BASIS API call ---

def fetch(section, session="34", chamber=None, queries=None, result_range=None,
          timeout=30.0):
    """Thin wrapper around query_basis.fetch_basis()."""
    return query_basis.fetch_basis(
        base_url=query_basis.DEFAULT_BASE_URL,
        section=section,
        session=session,
        chamber=chamber,
        queries=queries or [],
        result_range=result_range,
        version=query_basis.DEFAULT_VERSION,
        timeout=timeout,
    )


# --- Paginated, cached bill list ---

def fetch_all_bills(chamber, session="34", queries=None):
    """Fetch all bills for a chamber with optional expansions. Cached
    for 10 minutes."""
    cache_key = f"all_bills_v7_{session}_{chamber}_{','.join(queries or [])}"
    cached = _cache.get(cache_key, max_age=600)
    if cached is not None:
        return cached

    PAGE_SIZE = 100
    all_bills = []
    start = 0
    while True:
        end = start + PAGE_SIZE - 1
        range_str = f"{start}..{end}"
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
        if b["billnumber"] in seen:
            continue
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
    cached = _cache.get("all_actions_v5")
    if cached is not None:
        return cached

    bills = []
    for chamber in ["H", "S"]:
        seen = set()
        start = 0
        while True:
            end = start + 99
            range_str = f"{start}..{end}"
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

    _cache.put("all_actions_v5", bills)
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


def fetch_committee_schedule(date_str=None):
    """Scrape the akleg daily committee schedule for one date.

    date_str: 'M/D/YYYY' string; defaults to today (AKDT).
    Returns a list of meeting dicts:
        {chamber, committee_code, committee_name, committee_type,
         time, location, canceled, agenda}
    Each agenda item is {flags, billnumber, title, teleconferenced}.
    """
    if date_str is None:
        try:
            from zoneinfo import ZoneInfo
            now_ak = datetime.now(ZoneInfo("America/Anchorage"))
        except ImportError:
            now_ak = datetime.now()
        date_str = now_ak.strftime("%-m/%-d/%Y")

    cache_key = f"cmte_schedule_{date_str}"
    # Today: 60s; past: 24h (committees that already met don't change)
    try:
        target = datetime.strptime(date_str, "%m/%d/%Y").date()
    except ValueError:
        target = None
    today = datetime.now().date()
    if target and target < today:
        cache_age = 24 * 3600
    else:
        cache_age = 60
    cached = _cache.get(cache_key, max_age=cache_age)
    if cached is not None:
        return cached

    url = (
        f"{SCHEDULE_URL}?mode=results&type=&com=&"
        f"startDate={date_str}&endDate={date_str}&chamber="
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "basis-browser/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("schedule.fetch_failed date=%s err=%r", date_str, exc)
        return []

    # Split into meeting blocks on the <hr> separator rows.
    meeting_blocks = re.split(r'<tr><td\s+colspan="8"><hr></td></tr>', html)

    meetings = []
    for block in meeting_blocks:
        # Header: (H)COMMUNITY & REGIONAL AFFAIRS  Standing Committee  <code link>
        header = re.search(
            r'<td\s+colspan="2">\(([HS])\)([^<]+)</td>\s*'
            r'<td\s+colspan="2">([^<]+)<a\s+href="[^"]*code=([HS][A-Z0-9&]+)"',
            block,
        )
        if not header:
            continue
        chamber = header.group(1)
        committee_name = header.group(2).strip()
        committee_type = header.group(3).strip()
        committee_code_full = header.group(4)  # e.g. HCRA, SFIN
        committee_code = committee_code_full[1:]  # strip the H/S prefix

        # Date/time + location. Note: the legislature's HTML often omits
        # the closing </td> on the location cell, so we accept any
        # terminator (</td>, </tr>, or the next <tr>).
        time_loc = re.search(
            r'<td\s+colspan="2">((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+\s+\w+\s+\d+:\d+\s+[AP]M)</td>\s*<td>([^<]*?)(?:</td>|</tr>|<)',
            block,
        )
        if not time_loc:
            continue
        time_str = time_loc.group(1).strip()
        location = time_loc.group(2).strip()

        # Detect canceled / no meeting via agenda text
        canceled = bool(re.search(r'No Meeting Scheduled', block))

        # Agenda items — each <tr> with a teleconf cell.
        agenda = []
        # Use a tolerant regex over the row body.
        item_pattern = re.compile(
            r'<tr>\s*'
            r'<td\s+width=2%[^>]*>\s*([^<]*?)\s*</td>\s*'
            r'<td\s+nowrap>\s*(?:<a[^>]*\?Root=([^"]+)"[^>]*>([^<]+)</a>)?\s*</td>\s*'
            r'<td>\s*<nobr>\s*([^<]+?)\s*</nobr>\s*</td>\s*'
            r'<td\s+class="teleconf">\s*([^<]*?)\s*</td>',
            re.DOTALL,
        )
        for m in item_pattern.finditer(block):
            flags = m.group(1).strip()
            bn = " ".join(m.group(3).strip().split()) if m.group(3) else ""
            text = m.group(4).strip()
            tele = bool(m.group(5).strip())
            if not bn and not text:
                continue
            agenda.append({
                "flags": flags,
                "billnumber": bn,
                "title": text,
                "teleconferenced": tele,
            })

        # Decode HTML entities in human-readable strings.
        for a in agenda:
            a["title"] = _html.unescape(a["title"])
        meetings.append({
            "chamber": chamber,
            "committee_code": committee_code,
            "committee_name": _html.unescape(committee_name),
            "committee_type": committee_type,
            "time": time_str,
            "location": location,
            "canceled": canceled,
            "agenda": agenda,
        })

    # Sort by start time within day. Times look like "May 12 Tuesday 8:00 AM"
    def time_key(m):
        try:
            parts = m["time"].split()
            # Parse "May 12 Tuesday 8:00 AM" -> just the time
            t = datetime.strptime(f"{parts[3]} {parts[4]}", "%I:%M %p")
            return t.hour * 60 + t.minute
        except (ValueError, IndexError):
            return 9999

    meetings.sort(key=lambda m: (time_key(m), m["chamber"], m["committee_code"]))

    _cache.put(cache_key, meetings)
    return meetings


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
        start = 0
        while True:
            end = start + 99
            range_str = f"{start}..{end}"
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


# --- Canonical 'Bills Passed Both Bodies' list (scraped from akleg) ---

def fetch_passed_bills(session="34"):
    """Scrape the Alaska Legislature's canonical 'Bills Passed Both
    Bodies' list at /basis/Bill/Passed/{session}. This is the
    authoritative source for what passed both chambers — we use it
    as primary input and enrich from BASIS, rather than
    reverse-engineering passage state from action codes / status text.

    Returns list of dicts: {billnumber, title, sponsor, status,
    date, pdf_url}. Cached 5 min — list only updates when a bill
    clears both chambers.
    """
    cache_key = f"passed_bills_v1_{session}"
    cached = _cache.get(cache_key, max_age=300)
    if cached is not None:
        return cached

    url = f"https://www.akleg.gov/basis/Bill/Passed/{session}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "basis-browser/1"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning("passed_bills.fail err=%r", exc)
        return []

    # Each <tr class="Both ..."> = bill/resolution passed by both
    # chambers. The page also has Senate/House-only rows for
    # single-chamber resolutions; those don't go to the governor.
    rows = re.split(r'<tr class="Both[^"]*">', html_body)[1:]
    results = []
    bn_re = re.compile(r'<a href = "[^"]*Root=([^"]+)"[^>]*>[^<]+</a')
    title_re = re.compile(r'<td class="col02">([^<]*)</td>', re.DOTALL)
    sponsor_re = re.compile(
        r'<td class="col03">([^<]*?)(?:<BR>|<br>|</td>)', re.DOTALL,
    )
    status_re = re.compile(
        r'<td class="col04">(?:<nobr>)?'
        r"(?:<a [^>]*href='([^']+)'[^>]*>pdf</a>\s*)?"
        r"([^<]*?)(?:</nobr>|</td>)",
        re.DOTALL,
    )
    date_re = re.compile(r'<td class="col05">([^<]+)</td>')

    for chunk in rows:
        bn_m = bn_re.search(chunk)
        if not bn_m:
            continue
        bn = compact_billnumber(bn_m.group(1))
        title_m = title_re.search(chunk)
        sponsor_m = sponsor_re.search(chunk)
        status_m = status_re.search(chunk)
        date_m = date_re.search(chunk)
        pdf_url = (status_m.group(1).strip()
                   if status_m and status_m.group(1) else "")
        status_text = (status_m.group(2) if status_m else "").strip()
        # HTML-unescape every user-visible string. akleg's listing
        # leaves entities like '&amp;' / '&apos;' literal in the
        # source, and we used to pass them through to the template,
        # which then HTML-escaped them a second time — producing
        # 'LEGISLATIVE ETHICS CMTE &amp; PROCEEDINGS' in the UI.
        results.append({
            "billnumber": bn,
            "title": _html.unescape(title_m.group(1).strip()) if title_m else "",
            "sponsor": _html.unescape(sponsor_m.group(1).strip()) if sponsor_m else "",
            "status": _html.unescape(status_text),
            "date": (date_m.group(1).strip() if date_m else ""),
            "pdf_url": pdf_url,
        })

    _cache.put(cache_key, results)
    return results


# --- Single-bill detail fetch ---

def fetch_members(session="34"):
    """Member roster keyed by BASIS code. Each value carries name,
    chamber, party, district, and majority flag. Cached an hour."""
    cache_key = f"members_v4_{session}"
    cached = _cache.get(cache_key, max_age=3600)
    if cached is not None:
        return cached

    members = {}
    # BASIS members endpoint rejects ranges of 100+; also the open-form
    # "..50" returns the LAST 50 records alphabetically, not the first.
    # Always use an explicit "start..end" so pagination actually
    # advances forward through the roster.
    #
    # Committees expansion is cheap (a few extra elements per member)
    # and lets us derive chair/co-chair attributions for committee-
    # sponsored bills. The 'position' attribute on each <Committee>
    # element is 'C0'/'C1' for co-chairs/chairs, numeric for regular
    # members.
    PAGE = 50
    start = 0
    while True:
        end = start + PAGE - 1
        rng = f"{start}..{end}"
        r = fetch(section="members", session=session, result_range=rng,
                  queries=["Committees"])
        body = r["body"].decode("utf-8", errors="replace")
        if "<Error" in body[:300] or "FaultException" in body:
            break
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            break
        page_count = 0
        for member in root.iter():
            if strip_ns(member.tag) != "Member":
                continue
            det = None
            committees = []
            for child in member:
                tag = strip_ns(child.tag)
                if tag == "MemberDetails":
                    det = child
                elif tag == "Committees":
                    for c in child:
                        if strip_ns(c.tag) != "Committee":
                            continue
                        committees.append({
                            "chamber":  c.attrib.get("chamber", ""),
                            "code":     c.attrib.get("code", ""),
                            "position": c.attrib.get("position", ""),
                            "name":     (c.text or "").strip(),
                        })
            if det is None:
                continue
            code = det.attrib.get("code", "").strip()
            if not code or code in members:
                continue
            chamber = det.attrib.get("chamber", "")
            majority = det.attrib.get("majority") == "true"
            first = child_text(det, "FirstName") or ""
            last = child_text(det, "LastName") or ""
            party = child_text(det, "Party") or ""
            district = child_text(det, "District") or ""
            members[code] = {
                "code": code,
                "chamber": chamber,
                "name": (first + " " + last).strip(),
                "party": party,
                "district": district,
                "majority": majority,
                "committees": committees,
            }
            page_count += 1
        if page_count < PAGE:
            break
        start += PAGE
    _cache.put(cache_key, members)
    return members


def committee_chairs(session="34"):
    """Derive {"chamber|code": [chair_member_dict, ...]} by scanning
    every member's Committees list for 'position' starting with 'C'
    (BASIS's chair/co-chair marker). Multiple chairs per committee
    are common — House and Senate Finance each have two co-chairs.
    Key is "H|FIN" / "S|FIN" etc. as a flat string so the value is
    JSON-serializable (the disk cache stores JSON)."""
    cache_key = f"committee_chairs_v2_{session}"
    cached = _cache.get(cache_key, max_age=3600)
    if cached is not None:
        return cached

    chairs = {}
    for m in fetch_members(session).values():
        for c in m.get("committees") or []:
            pos = (c.get("position") or "").upper()
            if not pos.startswith("C"):
                continue
            key = f"{c.get('chamber','')}|{c.get('code','')}"
            chairs.setdefault(key, []).append({
                "code":     m["code"],
                "name":     m["name"],
                "party":    m["party"],
                "district": m["district"],
                "position": pos,
            })
    # Stable ordering: chairs first (C0/C1 lexical), then by last name.
    for k, lst in chairs.items():
        lst.sort(key=lambda x: (x["position"], x["name"].split()[-1]))
    _cache.put(cache_key, chairs)
    return chairs


def fetch_all_votes_index(session="34"):
    """Build a chamber-wide votes index: {billnumber: [vote, ...]}.

    Heavy operation — paginates the Votes expansion for both chambers
    in 10-bill chunks (larger pages routinely exceed the 30s socket
    timeout). Pays a one-time 60-90s cost in the background prefetch
    thread; cached for an hour. Once warm, per-bill votes lookups are
    a dict access in microseconds.
    """
    cache_key = f"all_votes_v2_{session}"
    cached = _cache.get(cache_key, max_age=3600)
    if cached is not None:
        return cached

    out = {}
    PAGE = 10  # bills per request — Votes is the slowest expansion in
               # BASIS; this is the largest size that reliably finishes.
    REQ_TIMEOUT = 60.0  # per-request socket timeout

    for chamber in ("H", "S"):
        start = 0
        while True:
            end = start + PAGE - 1
            rng = f"{start}..{end}"
            try:
                r = fetch(section="bills", session=session, chamber=chamber,
                          queries=["Votes"], result_range=rng,
                          timeout=REQ_TIMEOUT)
            except Exception as exc:
                log.warning("votes_index.fetch_failed chamber=%s rng=%s err=%r",
                            chamber, rng, exc)
                break
            body = r["body"].decode("utf-8", errors="replace")
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
                bn = compact_billnumber(bill.attrib.get("billnumber", ""))
                if not bn:
                    continue
                page_count += 1
                if bn in out:
                    continue
                votes = []
                for vote in bill.iter():
                    if strip_ns(vote.tag) != "Vote":
                        continue
                    member = ""
                    title = ""
                    date = ""
                    for ch in vote:
                        tag = strip_ns(ch.tag)
                        if tag == "Member":
                            member = (ch.text or "").strip()
                        elif tag == "Title":
                            title = (ch.text or "").strip()
                        elif tag == "Date":
                            date = (ch.text or "").strip()
                    votes.append({
                        "vote": vote.attrib.get("vote", "").strip(),
                        "member_code": member,
                        "title": title,
                        "date": date,
                    })
                out[bn] = votes
            if page_count < PAGE:
                break
            start += PAGE

    _cache.put(cache_key, out)
    return out


def fetch_bill_votes(billnumber, session="34"):
    """Look up one bill's votes from the chamber-wide index.

    NON-BLOCKING: if the index isn't already cached, returns an empty
    list rather than triggering a 3-minute build inside the request
    handler. The build is performed by _build_votes_index_async on its
    own daemon thread; once warm, results appear on the next refresh."""
    cache_key = f"all_votes_v2_{session}"
    idx = _cache.get(cache_key, max_age=3600)
    if idx is None:
        # Don't block the request handler. Empty roll call is preferable
        # to a 3-minute hang; the daemon is building in the background.
        return []
    return idx.get(billnumber.strip(), [])


# Scrapes the akleg bill-detail page for any <a> linking to a
# get_documents.asp PDF whose anchor text mentions "Sponsor Statement".
# Most-recent statement appears first in HTML source order.
_SPONSOR_PDF_RE = re.compile(
    r'href="(https://www\.akleg\.gov/basis/get_documents\.asp'
    r'\?session=\d+&(?:amp;)?docid=\d+)"[^>]*>'
    r'([^<]*Sponsor Statement[^<]*)<',
    re.IGNORECASE,
)


def _clean_sponsor_text(raw):
    """Strip the legislator letterhead/contact block from a sponsor-
    statement PDF and return the body. Most statements have a line
    matching 'Sponsor Statement' followed by the topic heading and
    body — we anchor on that and discard everything before."""
    if not raw:
        return ""
    m = re.search(r'Sponsor Statement[^\n]*\n', raw, re.IGNORECASE)
    body = raw[m.end():] if m else raw
    # Normalize whitespace: collapse runs of spaces, dedupe blank lines.
    body = re.sub(r'[ \t]+', ' ', body)
    body = re.sub(r'\n{3,}', '\n\n', body)
    # Drop lines that are clearly letterhead remnants (page-2 headers etc).
    drop_re = re.compile(
        r'^\s*(Representative|Senator|Chair,|Serving (House|Senate) District|'
        r'Session:|Interim:|State Capitol)',
        re.IGNORECASE,
    )
    lines = [ln for ln in body.splitlines() if not drop_re.search(ln)]
    return "\n".join(lines).strip()


def fetch_sponsor_statement(billnumber, session="34"):
    """Scrape the akleg bill page for the most recent Sponsor
    Statement PDF, download it, and extract the cleaned text body.

    Returns {'url': ..., 'text': ..., 'label': ...}. Empty strings if
    the bill has no statement on file. Cached for 24h since these
    documents don't change after passage."""
    bn = (billnumber or "").strip()
    if not bn:
        return {"url": "", "text": "", "label": ""}

    cache_key = f"sponsor_stmt_v1_{session}_{bn}"
    cached = _cache.get(cache_key, max_age=86400)
    if cached is not None:
        return cached

    result = {"url": "", "text": "", "label": ""}
    try:
        page_url = (
            f"https://www.akleg.gov/basis/Bill/Detail/{session}"
            f"?Root={bn.replace(' ', '%20')}"
        )
        req = urllib.request.Request(
            page_url, headers={"User-Agent": "basis-browser/1"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html_body = resp.read().decode("utf-8", errors="replace")
        matches = _SPONSOR_PDF_RE.findall(html_body)
        # Deduplicate by URL while preserving order — akleg sometimes
        # lists the same statement multiple times.
        seen = set()
        unique = []
        for href, label in matches:
            href = href.replace("&amp;", "&")
            if href in seen:
                continue
            seen.add(href)
            unique.append((href, label.strip()))
        if not unique:
            _cache.put(cache_key, result)
            return result
        pdf_url, label = unique[0]
        result["url"] = pdf_url
        result["label"] = label
        req2 = urllib.request.Request(
            pdf_url, headers={"User-Agent": "basis-browser/1"},
        )
        with urllib.request.urlopen(req2, timeout=25) as r2:
            pdf_bytes = r2.read()
        # pypdf is imported lazily — heavy dep only paid for here.
        import pypdf
        import io as _io
        reader = pypdf.PdfReader(_io.BytesIO(pdf_bytes))
        raw_text = "\n".join((p.extract_text() or "") for p in reader.pages)
        result["text"] = _clean_sponsor_text(raw_text)
    except Exception as exc:
        log.warning("sponsor_stmt.fail bn=%s err=%r", bn, exc)

    _cache.put(cache_key, result)
    return result


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
        # Use explicit forward ranges — "..100" returns LAST 100, not first.
        for rng in ["0..99", "100..199", "200..299", "300..399", "400..499"]:
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
