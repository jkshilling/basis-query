"""Thin wrapper around the repo's query_basis.fetch_basis()."""

import re
import sys
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import query_basis

SCHEDULE_URL = "https://www.akleg.gov/basis/Meeting/Index"


def _strip_ns(tag):
    return tag.rsplit("}", 1)[1] if "}" in tag else tag


def _child_text(elem, name):
    for child in elem:
        if _strip_ns(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _child_attr(elem, name, attr):
    for child in elem:
        if _strip_ns(child.tag) == name:
            return child.attrib.get(attr, "")
    return ""


def _format_status_date(date_str):
    """Turn '2026-04-13' into 'Apr 13'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%b %-d")
    except (ValueError, TypeError):
        return date_str


def _compact_billnumber(raw):
    """'HB 173' -> 'HB 173', 'HSCR 1' -> 'HSCR 1'. Collapse internal whitespace."""
    return " ".join(raw.split())


def _truncate(text, length=60):
    if len(text) <= length:
        return text
    return text[:length - 1].rstrip() + "\u2026"


def _compact_hearing(raw):
    """'Apr 21 Tuesday 3:30 PM' -> 'Apr 21, Tue, 3:30 PM'."""
    if not raw:
        return raw
    day_map = {
        "Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed",
        "Thursday": "Thu", "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun",
    }
    for full, abbr in day_map.items():
        raw = raw.replace(full, abbr)
    # Insert commas: "Apr 21 Tue 3:30 PM" -> "Apr 21, Tue, 3:30 PM"
    m = re.match(r'(\w+ \d+)\s+(\w+)\s+(.+)', raw)
    if m:
        return f"{m.group(1)}, {m.group(2)}, {m.group(3)}"
    return raw


def _fetch(section, session="34", chamber=None, queries=None, result_range=None):
    return query_basis.fetch_basis(
        base_url=query_basis.DEFAULT_BASE_URL,
        section=section,
        session=session,
        chamber=chamber,
        queries=queries or [],
        result_range=result_range,
        version=query_basis.DEFAULT_VERSION,
    )


def _next_referral(elem, current_code, other_chamber):
    """Find the next committee of referral after current_code.

    Looks at action code 091 in the other chamber for the referral list,
    then returns the committee after current_code, or '' if none.
    """
    for actions in elem:
        if _strip_ns(actions.tag) != "Actions":
            continue
        for action in actions:
            if (action.attrib.get("chamber", "") == other_chamber
                    and action.attrib.get("code", "") == "091"):
                referral_text = _child_text(action, "ActionText")
                codes = [c.strip() for c in referral_text.split(",")]
                try:
                    idx = codes.index(current_code)
                    if idx + 1 < len(codes):
                        return codes[idx + 1]
                except ValueError:
                    pass
    return ""


def parse_bills(result, other_chamber=None):
    body = result["body"].decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    bills = []
    for elem in root.iter():
        if _strip_ns(elem.tag) != "Bill":
            continue
        committee_code = _child_attr(elem, "CurrentCommittee", "committeecode")
        next_ref = ""
        if other_chamber:
            next_ref = _next_referral(elem, committee_code, other_chamber)
        bills.append({
            "billnumber": _compact_billnumber(elem.attrib.get("billnumber", "")),
            "chamber": elem.attrib.get("chamber", "").strip(),
            "short_title": _truncate(_child_text(elem, "ShortTitle")),
            "status": _child_text(elem, "StatusText"),
            "status_code": _child_attr(elem, "StatusText", "statuscode"),
            "status_date": _format_status_date(_child_text(elem, "StatusDate")),
            "committee": _child_text(elem, "CurrentCommittee"),
            "committee_code": committee_code,
            "next_referral": next_ref,
        })
    return bills


def _parse_hearing_datetime(raw, year=None):
    """Parse 'Apr 16 Wednesday 1:30 PM' into a datetime object."""
    if not year:
        year = datetime.now().year
    try:
        # Strip day name: "Apr 16 Wednesday 1:30 PM" -> "Apr 16 1:30 PM"
        parts = raw.split()
        # parts: ['Apr', '16', 'Wednesday', '1:30', 'PM']
        cleaned = f"{parts[0]} {parts[1]} {parts[3]} {parts[4]} {year}"
        return datetime.strptime(cleaned, "%b %d %I:%M %p %Y")
    except (ValueError, IndexError):
        return None


def _fetch_hearing_schedule(chamber="S", days=7):
    """Scrape the akleg hearing schedule HTML to build a map of
    bill_number -> next hearing datetime string.

    Uses the same endpoint the legislature website uses:
    /basis/Meeting/Index?mode=results&chamber=...&startDate=...&endDate=...

    Only includes hearings that haven't happened yet in AKDT.
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
            # Skip hearings that have already passed
            hearing_dt = _parse_hearing_datetime(current_datetime, year=now_ak.year)
            if hearing_dt and hearing_dt < now_ak:
                continue

            bill_num = bill_match.group(2).strip()
            if bill_num not in bill_hearings:
                bill_hearings[bill_num] = current_datetime

    return bill_hearings


def _crossover_bills(origin_chamber, session="34"):
    """Fetch bills from origin_chamber that have crossed to the other chamber."""
    other = "S" if origin_chamber == "H" else "H"
    status_marker = f"({other})"

    PAGE_SIZE = 100
    all_bills = []

    start = 1
    while True:
        end = start + PAGE_SIZE - 1
        range_str = f"{start}..{end}" if start > 1 else f"..{PAGE_SIZE}"
        result = _fetch(
            section="bills",
            session=session,
            chamber=origin_chamber,
            queries=["Actions"],
            result_range=range_str,
        )
        page = parse_bills(result, other_chamber=other)
        if not page:
            break
        all_bills.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    seen = set()
    crossed = []
    origin_marker = f"({origin_chamber})"
    for b in all_bills:
        key = b["billnumber"]
        if key in seen:
            continue
        seen.add(key)
        status = b["status"]
        # Smarter detection: a bill is "in the other chamber" if its status
        # places it there as the current location. Reject statuses that
        # explicitly show the bill back in its origin chamber, or where
        # `(H)` / `(S)` only appears as part of an amendment reference like
        # `FLD CONCUR(S)AM` or `CONCURRED(H) AM`.
        status_upper = status.upper()
        if origin_marker in status:
            # Bill currently shows origin chamber → it's back home.
            continue
        if "FLD CONCUR" in status_upper or "CONCURRED" in status_upper:
            # Concurrence outcomes mean the bill returned to its origin chamber.
            continue
        if status_marker in status or status_upper.startswith(f"TRANSMITTED TO ({other})"):
            crossed.append(b)
        elif status_upper == f"READ FIRST TIME ({other})":
            crossed.append(b)

    hearings = _fetch_hearing_schedule(chamber=other)
    for b in crossed:
        b["next_hearing"] = _compact_hearing(hearings.get(b["billnumber"], ""))

    return crossed


def house_bills_in_senate(session="34"):
    import cache
    cached = cache.get("hb_in_senate")
    if cached is not None:
        return cached
    result = _crossover_bills("H", session)
    cache.put("hb_in_senate", result)
    return result


def senate_bills_in_house(session="34"):
    import cache
    cached = cache.get("sb_in_house")
    if cached is not None:
        return cached
    result = _crossover_bills("S", session)
    cache.put("sb_in_house", result)
    return result


def _fetch_all_bills(chamber, session="34", queries=None):
    """Fetch all bills for a chamber with optional expansions. Cached."""
    import cache as _cache

    cache_key = f"all_bills_{session}_{chamber}_{','.join(queries or [])}"
    cached = _cache.get(cache_key, max_age=600)  # 10 minutes
    if cached is not None:
        return cached

    PAGE_SIZE = 100
    all_bills = []
    start = 1
    while True:
        end = start + PAGE_SIZE - 1
        range_str = f"{start}..{end}" if start > 1 else f"..{PAGE_SIZE}"
        result = _fetch(
            section="bills", session=session,
            chamber=chamber, queries=queries,
            result_range=range_str,
        )
        page = _parse_bills_extended(result) if queries else parse_bills(result)
        if not page:
            break
        all_bills.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE

    # Deduplicate
    seen = set()
    unique = []
    for b in all_bills:
        if b["billnumber"] not in seen:
            seen.add(b["billnumber"])
            unique.append(b)

    _cache.put(cache_key, unique)
    return unique


def _parse_bills_extended(result):
    """Parse bills with Sponsors and Versions expansions."""
    body = result["body"].decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    bills = []
    for elem in root.iter():
        if _strip_ns(elem.tag) != "Bill":
            continue

        # Base fields
        bill = {
            "billnumber": _compact_billnumber(elem.attrib.get("billnumber", "")),
            "chamber": elem.attrib.get("chamber", "").strip(),
            "short_title": _truncate(_child_text(elem, "ShortTitle")),
            "status": _child_text(elem, "StatusText"),
            "status_code": _child_attr(elem, "StatusText", "statuscode"),
            "status_date": _format_status_date(_child_text(elem, "StatusDate")),
            "committee": _child_text(elem, "CurrentCommittee"),
            "committee_code": _child_attr(elem, "CurrentCommittee", "committeecode"),
            "next_referral": "",
            "prime_sponsor": "",
            "sponsor_count": 0,
            "version_count": 0,
            "subjects": [],
        }

        # Subjects
        for subs in elem:
            if _strip_ns(subs.tag) != "Subjects":
                continue
            for sub in subs:
                if _strip_ns(sub.tag) == "Subject":
                    txt = (sub.text or "").strip()
                    if txt:
                        bill["subjects"].append(txt)

        # Sponsors
        for sponsors in elem:
            if _strip_ns(sponsors.tag) != "Sponsors":
                continue
            count = 0
            for member in sponsors:
                if _strip_ns(member.tag) == "MemberDetails":
                    count += 1
                    if member.attrib.get("primesponsor") == "true":
                        first = _child_text(member, "FirstName")
                        last = _child_text(member, "LastName")
                        bill["prime_sponsor"] = f"{first} {last}"
                elif _strip_ns(member.tag) == "Committee":
                    # Committee-sponsored bill
                    if not bill["prime_sponsor"]:
                        bill["prime_sponsor"] = f"({member.attrib.get('code', '')})"
                    count += 1
            bill["sponsor_count"] = count

        # Versions
        for versions in elem:
            if _strip_ns(versions.tag) != "Versions":
                continue
            ver_count = 0
            for ver in versions:
                if _strip_ns(ver.tag) == "Version":
                    letter = ver.attrib.get("versionletter", "")
                    if letter != "Z":  # Z is enrolled, not an amendment
                        ver_count += 1
            bill["version_count"] = ver_count

        bills.append(bill)
    return bills


def _fetch_committee_reports(session="34"):
    """Count bills reported out by each committee using action code 002.
    Reuses cached action data instead of re-fetching."""
    from collections import Counter
    reported = Counter()

    # Reuse _scan_all_actions cache — no duplicate API calls
    all_bills = _scan_all_actions(session)
    for bn, origin, title, status, actions in all_bills:
        for code, achamber, jdate, text in actions:
            if code == "002":
                cmte = text.split(" ")[0] if text else ""
                if cmte and achamber:
                    reported[f"({achamber}) {cmte}"] += 1
    return reported


def _fetch_hearing_window(session, start_date, end_date, cache_for_seconds):
    """Fetch hearings for a single 2-week window. Cached per-window."""
    import cache as _cache
    from collections import Counter

    s = start_date.strftime("%m/%d/%y")
    e = end_date.strftime("%m/%d/%y")
    key = f"hearings_{session}_{s}_{e}"

    cached = _cache.get(key, max_age=cache_for_seconds)
    if cached is not None:
        return Counter(cached)

    counts = Counter()
    try:
        result = _fetch(
            section="meetings", session=session,
            queries=[f"Meetings;startdate={s};enddate={e}"],
            result_range="..50",
        )
        body = result["body"].decode("utf-8", errors="replace")
        if len(body) > 300 and "FaultException" not in body:
            root = ET.fromstring(body)
            for elem in root.iter():
                if _strip_ns(elem.tag) != "Meeting":
                    continue
                if elem.attrib.get("Canceled") == "true":
                    continue
                chamber = _child_text(elem, "chamber")
                sponsor = _child_text(elem, "Sponsor")
                if chamber and sponsor:
                    counts[f"({chamber}) {sponsor}"] += 1
    except Exception:
        pass

    _cache.put(key, dict(counts))
    return counts


def _fetch_hearing_counts(session="34"):
    """Count hearings per committee across the full session using 2-week windows.
    Each window is cached separately. Past windows are immutable so they cache forever.
    The most recent two windows refresh hourly."""
    from collections import Counter
    import datetime as dt

    hearings = Counter()
    start = dt.date(2025, 1, 1)
    end_date = dt.date.today()
    today = dt.date.today()

    # Build list of windows
    windows = []
    cursor = start
    while cursor < end_date:
        window_end = min(cursor + dt.timedelta(days=13), end_date)
        windows.append((cursor, window_end))
        cursor = window_end + dt.timedelta(days=1)

    # Past windows (ending more than 14 days ago) cache for ~30 days; recent ones for 1 hour
    for ws, we in windows:
        days_old = (today - we).days
        if days_old > 14:
            cache_seconds = 30 * 24 * 3600  # 30 days
        else:
            cache_seconds = 3600  # 1 hour
        window_counts = _fetch_hearing_window(session, ws, we, cache_seconds)
        hearings.update(window_counts)

    return hearings


def dashboard_stats(session="34"):
    """Compute session-wide stats for the dashboard."""
    import cache
    from collections import Counter

    cached = cache.get("dashboard_stats")
    if cached is not None:
        return cached

    house_bills = _fetch_all_bills("H", session, queries=["Sponsors", "Versions"])
    senate_bills = _fetch_all_bills("S", session, queries=["Sponsors", "Versions"])

    total_house = len(house_bills)
    total_senate = len(senate_bills)

    # Bill type breakdown
    type_counts = Counter()
    crossover_to_senate = 0
    crossover_to_house = 0
    chaptered = 0
    at_governor = 0
    vetoed = 0
    veto_sustained = 0
    veto_overridden = 0
    failed = 0
    withdrawn = 0

    house_committee_counts = Counter()
    senate_committee_counts = Counter()
    sponsor_counts = Counter()

    all_bills = house_bills + senate_bills

    # Build a map of bill -> set of historical action codes from cached action data
    action_history = {}  # billnumber -> set of codes ever taken
    for bn, _, _, _, actions in _scan_all_actions(session):
        codes = set(c[0] for c in actions)
        action_history[_compact_billnumber(bn)] = codes

    for b in all_bills:
        status = b["status"]
        status_upper = status.upper()
        origin = b["chamber"]
        history = action_history.get(b["billnumber"], set())

        # Bill type
        prefix = b["billnumber"].split()[0] if b["billnumber"] else "?"
        type_counts[prefix] += 1

        # Crossover
        if origin == "H" and "(S)" in status:
            crossover_to_senate += 1
        if origin == "S" and "(H)" in status:
            crossover_to_house += 1

        # Veto outcomes — count from action history, since current status may
        # be "CHAPTER X SLA YY" once an overridden bill becomes law.
        if "038" in history:  # vetoed by governor
            if "040" in history:  # ...and override succeeded
                veto_overridden += 1
            elif "039" in history:  # ...and override failed
                veto_sustained += 1
            else:  # vetoed but no override action yet
                vetoed += 1

        # Other terminal statuses (current state)
        if "CHAPTER" in status_upper:
            chaptered += 1
        elif status_upper == "TRANSM TO GOVERNOR":
            at_governor += 1
        elif "FAILED" in status_upper:
            failed += 1
        elif status_upper == "WITHDRAWN":
            withdrawn += 1

        # Bills currently in committee
        if "(H)" in status and b["committee_code"]:
            house_committee_counts[b["committee_code"]] += 1
        if "(S)" in status and b["committee_code"]:
            senate_committee_counts[b["committee_code"]] += 1

        # Sponsors
        if b.get("prime_sponsor"):
            sponsor_counts[b["prime_sponsor"]] += 1

    # Most amended
    amended = [(b["billnumber"], b["short_title"], b["version_count"])
               for b in all_bills if b.get("version_count", 0) > 1]
    amended.sort(key=lambda x: x[2], reverse=True)
    most_amended = amended[:15]

    # Committee reports (bills moved out) — from Actions code 002
    reported = _fetch_committee_reports(session)

    # Build throughput: sitting + reported out
    def build_throughput(sitting_counts, chamber_prefix):
        reported_for_chamber = {
            k.split(") ")[1]: v for k, v in reported.items()
            if k.startswith(chamber_prefix)
        }
        all_cmtes = set(sitting_counts.keys()) | set(reported_for_chamber.keys())
        rows = []
        for code in all_cmtes:
            s = sitting_counts.get(code, 0)
            r = reported_for_chamber.get(code, 0)
            rows.append((code, s, r))
        rows.sort(key=lambda x: x[1] + x[2], reverse=True)
        return rows[:15]

    house_throughput = build_throughput(house_committee_counts, "(H)")
    senate_throughput = build_throughput(senate_committee_counts, "(S)")

    # Hearing counts
    hearing_counts = _fetch_hearing_counts(session)
    house_hearings = [(k.split(") ")[1], v) for k, v in hearing_counts.items() if k.startswith("(H)")]
    senate_hearings = [(k.split(") ")[1], v) for k, v in hearing_counts.items() if k.startswith("(S)")]
    house_hearings.sort(key=lambda x: x[1], reverse=True)
    senate_hearings.sort(key=lambda x: x[1], reverse=True)

    # Count actual bills vs resolutions
    bills_only = sum(v for k, v in type_counts.items() if k in ("HB", "SB"))
    resolutions = sum(v for k, v in type_counts.items() if k not in ("HB", "SB"))

    # Floor calendar — bills with CAL in status (excluding Secretary's desk)
    import re as _re
    house_floor = []
    senate_floor = []
    for b in all_bills:
        status = b["status"]
        status_upper = status.upper()
        if "CAL" not in status_upper or "SECY" in status_upper:
            continue
        # Determine which chamber's floor
        cal_match = _re.search(r'CAL\(([HS])\)', status)
        reading = ""
        if "3RD RDG" in status_upper or "THIRD" in status_upper:
            reading = "3rd Reading"
        elif "2ND" in status_upper or "SECOND" in status_upper:
            reading = "2nd Reading"
        elif "HELD" in status_upper:
            reading = "Held"
        entry = {
            "billnumber": b["billnumber"],
            "title": b["short_title"],
            "status": status,
            "reading": reading,
        }
        if cal_match:
            if cal_match.group(1) == "H":
                house_floor.append(entry)
            else:
                senate_floor.append(entry)
        elif "(H)" in status:
            house_floor.append(entry)
        elif "(S)" in status:
            senate_floor.append(entry)

    stats = {
        "total_house": total_house,
        "total_senate": total_senate,
        "total": total_house + total_senate,
        "bills_only": bills_only,
        "resolutions": resolutions,
        "type_counts": type_counts.most_common(),
        "crossover_to_senate": crossover_to_senate,
        "crossover_to_house": crossover_to_house,
        "chaptered": chaptered,
        "at_governor": at_governor,
        "vetoed": vetoed,
        "veto_sustained": veto_sustained,
        "veto_overridden": veto_overridden,
        "failed": failed,
        "withdrawn": withdrawn,
        "house_by_committee": house_committee_counts.most_common(15),
        "senate_by_committee": senate_committee_counts.most_common(15),
        "house_throughput": house_throughput,
        "senate_throughput": senate_throughput,
        "house_hearings": house_hearings[:15],
        "senate_hearings": senate_hearings[:15],
        "top_sponsors": [(k, v) for k, v in sponsor_counts.most_common(25) if k != "(RLS)"][:20],
        "most_amended": most_amended,
        "house_floor": house_floor,
        "senate_floor": senate_floor,
    }
    cache.put("dashboard_stats", stats)
    return stats


def _count_actions_by_year(session, years):
    """Count action codes by year for a given session."""
    from collections import Counter
    counts = {y: Counter() for y in years}

    for chamber in ["H", "S"]:
        start = 1
        while True:
            end = start + 99
            range_str = f"{start}..{end}" if start > 1 else "..100"
            result = _fetch(
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
                if _strip_ns(bill.tag) != "Bill":
                    continue
                page_count += 1
                for actions in bill:
                    if _strip_ns(actions.tag) != "Actions":
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


def action_code_counts():
    """Count action code occurrences by year (2023-2026) across sessions 33 and 34."""
    import cache

    cached = cache.get("action_code_counts")
    if cached is not None:
        return cached

    # Session 34 covers 2025-2026
    counts_34 = _count_actions_by_year("34", ["2025", "2026"])
    # Session 33 covers 2023-2024
    counts_33 = _count_actions_by_year("33", ["2023", "2024"])

    result = {
        "2023": counts_33.get("2023", {}),
        "2024": counts_33.get("2024", {}),
        "2025": counts_34.get("2025", {}),
        "2026": counts_34.get("2026", {}),
    }
    cache.put("action_code_counts", result)
    return result


def _scan_all_actions(session="34"):
    """Fetch all bills with Actions and return raw action data.
    Returns list of (billnumber, chamber, short_title, status, actions_list)
    where actions_list is [(code, chamber, journaldate, action_text), ...]
    """
    import cache

    cached = cache.get("all_actions")
    if cached is not None:
        return cached

    bills = []
    for chamber in ["H", "S"]:
        seen = set()
        start = 1
        while True:
            end = start + 99
            range_str = f"{start}..{end}" if start > 1 else "..100"
            result = _fetch(
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
                if _strip_ns(bill.tag) != "Bill":
                    continue
                bn = bill.attrib.get("billnumber", "").strip()
                if bn in seen:
                    continue
                seen.add(bn)
                page_count += 1
                short_title = _child_text(bill, "ShortTitle")
                status = _child_text(bill, "StatusText")
                bill_actions = []
                for actions_elem in bill:
                    if _strip_ns(actions_elem.tag) != "Actions":
                        continue
                    for action in actions_elem:
                        bill_actions.append((
                            action.attrib.get("code", ""),
                            action.attrib.get("chamber", ""),
                            action.attrib.get("journaldate", ""),
                            _child_text(action, "ActionText"),
                        ))
                bills.append((
                    _compact_billnumber(bn),
                    chamber,
                    _truncate(short_title),
                    status,
                    bill_actions,
                ))
            if page_count < 100:
                break
            start += 100

    cache.put("all_actions", bills)
    return bills


def bill_progress(session="34"):
    """Build bill progress data: velocity scores, concur/nonconcur."""
    import cache

    cached = cache.get("bill_progress")
    if cached is not None:
        return cached

    all_bills = _scan_all_actions(session)

    velocity = []
    concur_actions = []

    for bn, origin, title, status, actions in all_bills:
        # Bills only
        prefix = bn.split()[0] if bn else ""
        if prefix not in ("HB", "SB"):
            # Still capture concur actions for resolutions
            for code, achamber, jdate, text in actions:
                if code == "026":
                    concur_actions.append({
                        "billnumber": bn, "title": title, "chamber": achamber,
                        "date": _format_status_date(jdate), "action": "concur", "text": text,
                    })
                elif code == "027":
                    concur_actions.append({
                        "billnumber": bn, "title": title, "chamber": achamber,
                        "date": _format_status_date(jdate), "action": "nonconcur", "text": text,
                    })
            continue

        # Build ordered list of milestones for this bill
        intro_date = None
        referrals = ""
        cmte_reports = []  # [(committee_code, date, report_text)]
        steps = []  # [(label, date)] in chronological order

        for code, achamber, jdate, text in actions:
            if achamber != origin:
                continue

            if code == "001" and not intro_date:
                intro_date = jdate
                steps.append(("Introduced", jdate))
            elif code == "091" and not referrals:
                referrals = text
            elif code == "002":
                cmte = text.split(" ")[0] if text else "?"
                cmte_reports.append((cmte, jdate, text))
                steps.append((f"Rpt: {cmte}", jdate))
            elif code == "008":
                steps.append(("2nd Reading", jdate))
            elif code == "016":
                steps.append(("3rd Reading", jdate))
            elif code == "020":
                steps.append(("1st Body Passage", jdate))

            # Concur / nonconcur
            if code == "026":
                concur_actions.append({
                    "billnumber": bn, "title": title, "chamber": achamber,
                    "date": _format_status_date(jdate), "action": "concur", "text": text,
                })
            elif code == "027":
                concur_actions.append({
                    "billnumber": bn, "title": title, "chamber": achamber,
                    "date": _format_status_date(jdate), "action": "nonconcur", "text": text,
                })

        if not intro_date:
            continue

        # Compute incremental days between consecutive steps
        step_days = []
        for i, (label, jdate) in enumerate(steps):
            inc = None
            if i > 0:
                try:
                    d1 = datetime.strptime(steps[i - 1][1], "%Y-%m-%d")
                    d2 = datetime.strptime(jdate, "%Y-%m-%d")
                    inc = (d2 - d1).days
                except ValueError:
                    pass
            step_days.append({
                "label": label,
                "date": _format_status_date(jdate),
                "inc_days": inc,
            })

        # Total days from intro to last step
        total_days = None
        if len(steps) > 1:
            try:
                d1 = datetime.strptime(steps[0][1], "%Y-%m-%d")
                d2 = datetime.strptime(steps[-1][1], "%Y-%m-%d")
                total_days = (d2 - d1).days
            except ValueError:
                pass

        velocity.append({
            "billnumber": bn,
            "title": title,
            "origin": origin,
            "status": status,
            "referrals": referrals,
            "steps": step_days,
            "raw_steps": steps,  # [(label, raw_date)]
            "total_days": total_days,
            "step_count": len(steps),
        })

    # Sort velocity: bills that went furthest and fastest first
    velocity.sort(key=lambda x: (-x["step_count"], x["total_days"] or 9999))

    # Momentum: bills with the most milestone activity in the last 30 days
    cutoff_30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    momentum = []
    for v in velocity:
        recent_steps = [s for s in v["raw_steps"] if s[1] >= cutoff_30]
        if not recent_steps:
            continue
        # Score: number of recent milestones, tiebreak by most recent date
        most_recent = max(s[1] for s in recent_steps)
        momentum.append({
            "billnumber": v["billnumber"],
            "title": v["title"],
            "origin": v["origin"],
            "status": v["status"],
            "referrals": v["referrals"],
            "recent_count": len(recent_steps),
            "most_recent": _format_status_date(most_recent),
            "most_recent_raw": most_recent,
            "recent_steps": [
                {"label": s[0], "date": _format_status_date(s[1])}
                for s in recent_steps
            ],
            "total_steps": v["step_count"],
        })
    momentum.sort(key=lambda x: (-x["recent_count"], x["most_recent_raw"]), reverse=False)
    momentum.sort(key=lambda x: -x["recent_count"])

    # Weighted momentum: only count meaningful progress events
    # Committee reports = real progress. Floor steps after 2nd reading = procedural.
    # Collapse 2nd reading -> 3rd reading -> passage -> transmitted into one "floor passage" event.
    weighted = []
    for v in velocity:
        events = []  # (label, raw_date) — meaningful events only
        floor_date = None
        for label, raw_date in v["raw_steps"]:
            if label == "Introduced":
                events.append(("Introduced", raw_date))
            elif label.startswith("Rpt:"):
                events.append((label, raw_date))
            elif label in ("2nd Reading", "3rd Reading", "1st Body Passage"):
                # Take the earliest floor step as the single "floor passage" event
                if floor_date is None:
                    floor_date = raw_date
                    events.append(("Floor Passage", raw_date))

        if len(events) < 2:
            continue

        # Compute incremental days between meaningful events
        event_days = []
        for i, (label, raw_date) in enumerate(events):
            inc = None
            if i > 0:
                try:
                    d1 = datetime.strptime(events[i - 1][1], "%Y-%m-%d")
                    d2 = datetime.strptime(raw_date, "%Y-%m-%d")
                    inc = (d2 - d1).days
                except ValueError:
                    pass
            event_days.append({
                "label": label,
                "date": _format_status_date(raw_date),
                "inc_days": inc,
            })

        # Recent meaningful events in last 30 days
        recent_events = [e for e in events if e[1] >= cutoff_30 and e[0] != "Introduced"]
        most_recent_raw = max((e[1] for e in events), default="")

        # Total days intro to last meaningful event
        total_days = None
        if len(events) > 1:
            try:
                d1 = datetime.strptime(events[0][1], "%Y-%m-%d")
                d2 = datetime.strptime(events[-1][1], "%Y-%m-%d")
                total_days = (d2 - d1).days
            except ValueError:
                pass

        weighted.append({
            "billnumber": v["billnumber"],
            "title": v["title"],
            "origin": v["origin"],
            "referrals": v["referrals"],
            "events": event_days,
            "event_count": len(events),
            "recent_count": len(recent_events),
            "most_recent": _format_status_date(most_recent_raw),
            "most_recent_raw": most_recent_raw,
            "total_days": total_days,
        })

    # Sort: most recent meaningful events first, tiebreak by recency
    weighted.sort(key=lambda x: (-x["recent_count"], x["most_recent_raw"]))
    weighted.reverse()
    weighted.sort(key=lambda x: -x["recent_count"])

    concur_actions.sort(key=lambda x: x["date"], reverse=True)

    result = {
        "velocity": velocity[:50],
        "momentum": momentum[:50],
        "weighted": weighted[:50],
        "concur": concur_actions,
    }
    cache.put("bill_progress", result)
    return result


def governor_bills(session="34"):
    """Find all Governor-introduced bills and compute stats."""
    import cache
    from collections import Counter

    cached = cache.get("governor_bills")
    if cached is not None:
        return cached

    all_bills = _scan_all_actions(session)

    bills = []
    status_counts = Counter()
    committee_counts = Counter()

    for bn, origin, title, status, actions in all_bills:
        has_gov_letter = any(code == "050" for code, _, _, _ in actions)
        if not has_gov_letter:
            continue
        if title.upper().startswith("APPROP:"):
            continue

        referrals = ""
        intro_date = ""
        last_action_date = ""
        last_action_text = ""
        cmte_reports = 0

        for code, achamber, jdate, text in actions:
            if code == "091" and achamber == origin and not referrals:
                referrals = text
            if code == "001" and not intro_date:
                intro_date = jdate
            if jdate > last_action_date:
                last_action_date = jdate
                last_action_text = text
            if code == "002":
                cmte_reports += 1

        # Categorize status
        status_upper = status.upper()
        if "CHAPTER" in status_upper:
            category = "Chaptered"
        elif "TRANSM TO GOV" in status_upper:
            category = "At Governor"
        elif "VETOED" in status_upper:
            category = "Vetoed"
        elif "VETO SUSTAINED" in status_upper:
            category = "Veto Sustained"
        elif "FAILED" in status_upper:
            category = "Failed"
        elif "WITHDRAWN" in status_upper:
            category = "Withdrawn"
        elif "(S)" in status and origin == "H":
            category = "Crossed to Senate"
        elif "(H)" in status and origin == "S":
            category = "Crossed to House"
        else:
            category = "In Committee"

        status_counts[category] += 1

        # Current committee
        # Extract from status like "(H) FIN" -> "FIN"
        import re as _re
        cmte_match = _re.search(r'\([HS]\)\s*(\S+)', status)
        if cmte_match:
            committee_counts[f"({status[status.index('(')+1]}) {cmte_match.group(1)}"] += 1

        bills.append({
            "billnumber": _compact_billnumber(bn),
            "origin": origin,
            "title": _truncate(title, 40),
            "status": status,
            "category": category,
            "referrals": referrals,
            "intro_date": _format_status_date(intro_date),
            "last_action": _format_status_date(last_action_date),
            "last_action_text": _truncate(last_action_text, 40),
            "cmte_reports": cmte_reports,
        })

    # Scan hearing schedule for Governor bill appearances
    import datetime as dt
    bill_hearing_counts = Counter()
    gov_billnumbers = set(b["billnumber"] for b in bills)

    today = dt.date.today()
    start = dt.date(2025, 1, 1)
    end_date = today
    while start < end_date:
        window_end = min(start + dt.timedelta(days=13), end_date)
        s = start.strftime("%m/%d/%y")
        e = window_end.strftime("%m/%d/%y")

        # Cache each window's bill list separately; past windows cache for 30 days
        days_old = (today - window_end).days
        cache_key = f"sched_bills_{s}_{e}"
        cache_max_age = 30 * 24 * 3600 if days_old > 14 else 3600
        cached_bills = cache.get(cache_key, max_age=cache_max_age)

        if cached_bills is None:
            cached_bills = []
            try:
                url = (
                    f"{SCHEDULE_URL}?mode=results&type=&com=&"
                    f"startDate={s}&endDate={e}&chamber="
                )
                req = urllib.request.Request(url, headers={"User-Agent": "basis-browser/0.1"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                for m in re.finditer(r'Bill/Detail/\?Root=([^"]+)"[^>]*>([^<]+)</a>', html):
                    bn = " ".join(m.group(2).strip().split())
                    cached_bills.append(bn)
            except Exception:
                pass
            cache.put(cache_key, cached_bills)

        for bn in cached_bills:
            if bn in gov_billnumbers:
                bill_hearing_counts[bn] += 1

        start = window_end + dt.timedelta(days=1)

    # Add hearing count to each bill
    for b in bills:
        b["hearings"] = bill_hearing_counts.get(b["billnumber"], 0)

    total_hearings = sum(bill_hearing_counts.values())
    bills_with_hearings = sum(1 for b in bills if b["hearings"] > 0)
    total_moveouts = sum(b["cmte_reports"] for b in bills)
    bills_with_moveouts = sum(1 for b in bills if b["cmte_reports"] > 0)

    result = {
        "bills": bills,
        "total": len(bills),
        "status_counts": dict(status_counts),
        "committee_counts": committee_counts.most_common(15),
        "total_hearings": total_hearings,
        "bills_with_hearings": bills_with_hearings,
        "total_moveouts": total_moveouts,
        "bills_with_moveouts": bills_with_moveouts,
    }
    cache.put("governor_bills", result)
    return result


# Significant action codes for the activity feed
_NOTABLE_CODES = {
    "002": "Committee Report",
    "003": "Referred to Committee",
    "008": "Second Reading",
    "009": "CS Adopted (UC)",
    "015": "Advanced to Third Reading",
    "016": "Third Reading",
    "020": "Floor Vote",
    "022": "Transmitted to Other Chamber",
    "024": "Passed on Reconsideration",
    "026": "Concur Amendment",
    "027": "Failed to Concur",
    "029": "Conference Committee Appointed",
    "032": "Conference Report Adopted",
    "033": "Transmitted to Governor",
    "034": "Signed into Law",
    "036": "Law Without Signature",
    "038": "Vetoed by Governor",
    "039": "Veto Sustained",
    "040": "Veto Overridden",
    "051": "Returned to Committee",
    "053": "Withdrawn",
    "056": "Failed to Adopt CS",
    "060": "Failed Passage",
    "083": "Transmitted as Amended",
    "122": "CS Adopted (Roll Call)",
}


def activity_feed(session="34", days=7):
    """Build a recent activity feed of significant legislative actions."""
    import cache

    cache_key = f"activity_feed_{days}"
    cached = cache.get(cache_key, max_age=300)
    if cached is not None:
        return cached

    all_bills = _scan_all_actions(session)

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    events = []
    for bn, origin, title, status, actions in all_bills:
        for code, achamber, jdate, text in actions:
            if code not in _NOTABLE_CODES:
                continue
            if jdate < cutoff:
                continue
            events.append({
                "billnumber": bn,
                "title": title,
                "origin": origin,
                "action_chamber": achamber,
                "date": jdate,
                "date_display": _format_status_date(jdate),
                "code": code,
                "label": _NOTABLE_CODES[code],
                "text": text,
            })

    # Sort newest first
    events.sort(key=lambda x: (x["date"], x["code"]), reverse=True)

    # Group by date for display
    grouped = {}
    for e in events:
        d = e["date_display"]
        if d not in grouped:
            grouped[d] = []
        grouped[d].append(e)

    result = {
        "events": events,
        "grouped": grouped,
        "days": days,
        "total": len(events),
    }
    cache.put(cache_key, result)
    return result


# Map of action codes to short labels for the bill detail timeline.
_ACTION_LABELS = {
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


def _fetch_bill_detail(billnumber, session="34"):
    """Fetch one bill with all expansions for the detail page."""
    import cache as _cache
    key = f"bill_detail_{session}_{billnumber}"
    cached = _cache.get(key, max_age=600)
    if cached is not None:
        return cached

    # Determine chamber from prefix
    prefix = billnumber.strip().split()[0] if billnumber else ""
    chamber = "H" if prefix.startswith("H") else "S"

    # Build Bills query filter — fetch only this bill
    queries = [
        f"Bills;billnumber={billnumber}",
        "Actions",
        "Sponsors",
        "Versions",
        "Subjects",
    ]
    result = _fetch(
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
        if _strip_ns(elem.tag) == "Bill":
            if elem.attrib.get("billnumber", "").strip() == billnumber.strip() or \
               _compact_billnumber(elem.attrib.get("billnumber", "")) == billnumber.strip():
                bill_elem = elem
                break
    if bill_elem is None:
        # Filter sometimes returns wrong bill; scan all bills in this chamber instead.
        for rng in ["..100", "101..200", "201..300", "301..400", "401..500"]:
            rr = _fetch(
                section="bills", session=session, chamber=chamber,
                queries=["Actions", "Sponsors", "Versions", "Subjects"],
                result_range=rng,
            )
            b = rr["body"].decode("utf-8", errors="replace")
            if len(b) < 300 or "FaultException" in b:
                break
            r2 = ET.fromstring(b)
            for elem in r2.iter():
                if _strip_ns(elem.tag) != "Bill":
                    continue
                if _compact_billnumber(elem.attrib.get("billnumber", "")) == billnumber.strip():
                    bill_elem = elem
                    break
            if bill_elem is not None:
                break

    if bill_elem is None:
        return None

    # Collect everything
    bn = _compact_billnumber(bill_elem.attrib.get("billnumber", ""))
    detail = {
        "billnumber": bn,
        "chamber": bill_elem.attrib.get("chamber", "").strip(),
        "short_title": _child_text(bill_elem, "ShortTitle"),
        "status": _child_text(bill_elem, "StatusText"),
        "status_date": _format_status_date(_child_text(bill_elem, "StatusDate")),
        "committee": _child_text(bill_elem, "CurrentCommittee"),
        "committee_code": _child_attr(bill_elem, "CurrentCommittee", "committeecode"),
        "sponsors": [],
        "cosponsors": [],
        "committee_sponsor": "",
        "subjects": [],
        "versions": [],
        "actions": [],
        "fiscal_notes": [],
    }

    # Sponsors
    for sponsors in bill_elem:
        if _strip_ns(sponsors.tag) != "Sponsors":
            continue
        for member in sponsors:
            if _strip_ns(member.tag) == "MemberDetails":
                first = _child_text(member, "FirstName")
                last = _child_text(member, "LastName")
                party = _child_text(member, "Party")
                district = _child_text(member, "District")
                rec = {
                    "name": f"{first} {last}".strip(),
                    "party": party,
                    "district": district,
                }
                if member.attrib.get("primesponsor") == "true":
                    detail["sponsors"].append(rec)
                else:
                    detail["cosponsors"].append(rec)
            elif _strip_ns(member.tag) == "Committee":
                detail["committee_sponsor"] = member.attrib.get("code", "")

    # Subjects
    for subs in bill_elem:
        if _strip_ns(subs.tag) != "Subjects":
            continue
        for sub in subs:
            if _strip_ns(sub.tag) == "Subject":
                txt = (sub.text or "").strip()
                if txt:
                    detail["subjects"].append(txt)

    # Versions
    for vers in bill_elem:
        if _strip_ns(vers.tag) != "Versions":
            continue
        for v in vers:
            if _strip_ns(v.tag) != "Version":
                continue
            detail["versions"].append({
                "letter": v.attrib.get("versionletter", ""),
                "name": v.attrib.get("name", ""),
                "intro_date": _format_status_date(v.attrib.get("introdate", "")),
                "title": _child_text(v, "Title"),
            })

    # Actions (full timeline)
    for acts in bill_elem:
        if _strip_ns(acts.tag) != "Actions":
            continue
        for action in acts:
            if _strip_ns(action.tag) != "Action":
                continue
            code = action.attrib.get("code", "")
            text = _child_text(action, "ActionText")
            jdate = action.attrib.get("journaldate", "")
            achamber = action.attrib.get("chamber", "")
            label = _ACTION_LABELS.get(code, "Action")
            detail["actions"].append({
                "code": code,
                "label": label,
                "chamber": achamber,
                "date": _format_status_date(jdate),
                "raw_date": jdate,
                "text": text,
            })
            # Fiscal notes (codes 105-108)
            if code in ("105", "106", "107", "108"):
                detail["fiscal_notes"].append({
                    "date": _format_status_date(jdate),
                    "text": text,
                })

    # Sort actions chronologically (newest first)
    detail["actions"].sort(key=lambda a: a["raw_date"], reverse=True)

    _cache.put(key, detail)
    return detail


def committee_detail(chamber, code, session="34"):
    """Build committee detail data: bills currently in committee, recent meetings."""
    import cache as _cache
    key = f"cmte_detail_{chamber}_{code}"
    cached = _cache.get(key, max_age=600)
    if cached is not None:
        return cached

    all_bills = _scan_all_actions(session)
    bills_here = []
    for bn, origin, title, status, actions in all_bills:
        # Bill currently in this committee?
        marker = f"({chamber}) {code}"
        if marker in status:
            # Get its referral list (origin chamber)
            referrals = ""
            for c, a, d, t in actions:
                if c == "091" and a == origin and not referrals:
                    referrals = t
            bills_here.append({
                "billnumber": _compact_billnumber(bn),
                "title": _truncate(title, 50),
                "status": status,
                "referrals": referrals,
            })

    # Sort by bill number
    bills_here.sort(key=lambda b: b["billnumber"])

    result = {
        "chamber": chamber,
        "code": code,
        "bills": bills_here,
    }
    _cache.put(key, result)
    return result


def top_subjects(session="34", limit=10):
    """Aggregate subject counts across all bills (with Subjects expansion)."""
    import cache as _cache
    from collections import Counter

    cached = _cache.get("top_subjects", max_age=3600)
    if cached is not None:
        return cached

    counts = Counter()
    for chamber in ["H", "S"]:
        bills = _fetch_all_bills(chamber, session, queries=["Subjects"])
        for b in bills:
            for s in b.get("subjects", []):
                counts[s] += 1

    result = counts.most_common(limit)
    _cache.put("top_subjects", result)
    return result
