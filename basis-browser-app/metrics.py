"""Derived data: aggregations, scoring, and search built on top of fetch+parse.

Imports from fetch.py and parse.py. No I/O of its own — all API calls go
through fetch.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime, timedelta

import cache as _cache
from parse import (
    compact_billnumber, current_chamber, format_status_date,
    format_status_date_full, truncate,
)
from fetch import (
    fetch_all_bills, fetch_hearing_schedule, fetch_hearing_counts,
    fetch_committee_reports, scan_all_actions, count_actions_by_year,
    fetch_bill_detail, fetch_floor_calendar, is_procedural_resolution,
    fetch_members, fetch_bill_votes, fetch_sponsor_statement,
    fetch_passed_bills,
)
from bill_summaries import get_bill_summary


# --- Crossover bills (House↔Senate) ---

def _crossover_bills(origin_chamber, session="34"):
    other = "S" if origin_chamber == "H" else "H"
    status_marker = f"({other})"

    bills = fetch_all_bills(origin_chamber, session, queries=["Actions"])
    seen = set()
    crossed = []
    origin_marker = f"({origin_chamber})"
    for b in bills:
        key = b["billnumber"]
        if key in seen:
            continue
        seen.add(key)
        status = b["status"]
        status_upper = status.upper()
        if origin_marker in status:
            continue
        if "FLD CONCUR" in status_upper or "CONCURRED" in status_upper:
            continue
        if status_marker in status or status_upper.startswith(f"TRANSMITTED TO ({other})"):
            crossed.append(b)
        elif status_upper == f"READ FIRST TIME ({other})":
            crossed.append(b)

    # For each crossed bill, look up the destination chamber's referral
    # list (code 091 in the other chamber) and surface both the next
    # committee AND the full chain so callers can render whichever is
    # most useful.
    actions_by_bill = {}
    for bn, _, _, _, actions in scan_all_actions(session):
        actions_by_bill[compact_billnumber(bn)] = actions

    for b in crossed:
        bn = b["billnumber"]
        current_code = b.get("committee_code") or ""
        actions = actions_by_bill.get(bn, [])
        next_ref = ""
        chain = []
        for code, achamber, _jdate, text in actions:
            if code == "091" and achamber == other:
                chain = [c.strip() for c in text.split(",") if c.strip()]
                try:
                    idx = chain.index(current_code)
                    if idx + 1 < len(chain):
                        next_ref = chain[idx + 1]
                except ValueError:
                    pass
                break
        b["next_referral"] = next_ref
        b["referral_chain"] = chain

    from parse import compact_hearing
    hearings = fetch_hearing_schedule(chamber=other)
    for b in crossed:
        b["next_hearing"] = compact_hearing(hearings.get(b["billnumber"], ""))

    return crossed


def house_bills_in_senate(session="34"):
    cached = _cache.get("hb_in_senate")
    if cached is not None:
        return cached
    result = _crossover_bills("H", session)
    _cache.put("hb_in_senate", result)
    return result


def senate_bills_in_house(session="34"):
    cached = _cache.get("sb_in_house")
    if cached is not None:
        return cached
    result = _crossover_bills("S", session)
    _cache.put("sb_in_house", result)
    return result


# --- Dashboard ---

def dashboard_stats(session="34"):
    """Compute session-wide stats for the dashboard."""
    cached = _cache.get("dashboard_stats")
    if cached is not None:
        return cached

    house_bills = fetch_all_bills("H", session, queries=["Sponsors", "Versions"])
    senate_bills = fetch_all_bills("S", session, queries=["Sponsors", "Versions"])
    total_house = len(house_bills)
    total_senate = len(senate_bills)

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

    # Map of bill -> set of historical action codes (for veto detection).
    action_history = {}
    for bn, _, _, _, actions in scan_all_actions(session):
        action_history[compact_billnumber(bn)] = set(c[0] for c in actions)

    for b in all_bills:
        status = b["status"]
        status_upper = status.upper()
        origin = b["chamber"]
        history = action_history.get(b["billnumber"], set())

        prefix = b["billnumber"].split()[0] if b["billnumber"] else "?"
        type_counts[prefix] += 1

        loc = current_chamber(status, origin)
        if origin == "H" and loc == "S":
            crossover_to_senate += 1
        if origin == "S" and loc == "H":
            crossover_to_house += 1

        # Veto outcomes from action history.
        if "038" in history:
            if "040" in history:
                veto_overridden += 1
            elif "039" in history:
                veto_sustained += 1
            else:
                vetoed += 1

        if "CHAPTER" in status_upper:
            chaptered += 1
        elif status_upper == "TRANSM TO GOVERNOR":
            at_governor += 1
        elif "FAILED" in status_upper:
            failed += 1
        elif status_upper == "WITHDRAWN":
            withdrawn += 1

        if "(H)" in status and b["committee_code"]:
            house_committee_counts[b["committee_code"]] += 1
        if "(S)" in status and b["committee_code"]:
            senate_committee_counts[b["committee_code"]] += 1

        if b.get("prime_sponsor"):
            sponsor_counts[b["prime_sponsor"]] += 1

    # Most amended (currently unused on dashboard but kept for API).
    amended = [(b["billnumber"], b["short_title"], b["version_count"])
               for b in all_bills if b.get("version_count", 0) > 1]
    amended.sort(key=lambda x: x[2], reverse=True)
    most_amended = amended[:15]

    reported = fetch_committee_reports(session)

    def build_throughput(sitting_counts, chamber_prefix):
        reported_for_chamber = {
            k.split(") ")[1]: v for k, v in reported.items()
            if k.startswith(chamber_prefix)
        }
        all_cmtes = set(sitting_counts.keys()) | set(reported_for_chamber.keys())
        rows = [(code, sitting_counts.get(code, 0), reported_for_chamber.get(code, 0))
                for code in all_cmtes]
        rows.sort(key=lambda x: x[1] + x[2], reverse=True)
        return rows[:15]

    house_throughput = build_throughput(house_committee_counts, "(H)")
    senate_throughput = build_throughput(senate_committee_counts, "(S)")

    hearing_counts = fetch_hearing_counts(session)
    house_hearings = sorted(
        ((k.split(") ")[1], v) for k, v in hearing_counts.items() if k.startswith("(H)")),
        key=lambda x: x[1], reverse=True,
    )
    senate_hearings = sorted(
        ((k.split(") ")[1], v) for k, v in hearing_counts.items() if k.startswith("(S)")),
        key=lambda x: x[1], reverse=True,
    )

    bills_only = sum(v for k, v in type_counts.items() if k in ("HB", "SB"))
    resolutions = sum(v for k, v in type_counts.items() if k not in ("HB", "SB"))

    # Floor calendar — scrape the canonical akleg floor.asp page for each
    # chamber so we get exactly what the legislature publishes (including
    # bills that don't have "CAL" in their BASIS StatusText, like ones
    # returned to 2nd reading for amendments).
    house_floor = fetch_floor_calendar("H")
    senate_floor = fetch_floor_calendar("S")

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
    _cache.put("dashboard_stats", stats)
    return stats


# --- Action codes ---

def action_code_counts():
    """Count action code occurrences by year (2023-2026) across sessions 33 and 34."""
    cached = _cache.get("action_code_counts")
    if cached is not None:
        return cached
    counts_34 = count_actions_by_year("34", ["2025", "2026"])
    counts_33 = count_actions_by_year("33", ["2023", "2024"])
    result = {
        "2023": counts_33.get("2023", {}),
        "2024": counts_33.get("2024", {}),
        "2025": counts_34.get("2025", {}),
        "2026": counts_34.get("2026", {}),
    }
    _cache.put("action_code_counts", result)
    return result


# --- Bill progress (velocity / momentum) ---

def bill_progress(session="34"):
    """Velocity, momentum, and concur/nonconcur tables for bills."""
    cached = _cache.get("bill_progress")
    if cached is not None:
        return cached

    all_bills = scan_all_actions(session)
    velocity = []
    concur_actions = []

    for bn, origin, title, status, actions in all_bills:
        prefix = bn.split()[0] if bn else ""
        if prefix not in ("HB", "SB"):
            for code, achamber, jdate, text in actions:
                if code == "026":
                    concur_actions.append({
                        "billnumber": bn, "title": title, "chamber": achamber,
                        "date": format_status_date(jdate), "action": "concur", "text": text,
                    })
                elif code == "027":
                    concur_actions.append({
                        "billnumber": bn, "title": title, "chamber": achamber,
                        "date": format_status_date(jdate), "action": "nonconcur", "text": text,
                    })
            continue

        intro_date = None
        referrals = ""
        steps = []  # [(label, raw_date)]
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
                steps.append((f"Rpt: {cmte}", jdate))
            elif code == "008":
                steps.append(("2nd Reading", jdate))
            elif code == "016":
                steps.append(("3rd Reading", jdate))
            elif code == "020":
                steps.append(("1st Body Passage", jdate))
            if code == "026":
                concur_actions.append({
                    "billnumber": bn, "title": title, "chamber": achamber,
                    "date": format_status_date(jdate), "action": "concur", "text": text,
                })
            elif code == "027":
                concur_actions.append({
                    "billnumber": bn, "title": title, "chamber": achamber,
                    "date": format_status_date(jdate), "action": "nonconcur", "text": text,
                })
        if not intro_date:
            continue

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
            step_days.append({"label": label, "date": format_status_date(jdate), "inc_days": inc})

        total_days = None
        if len(steps) > 1:
            try:
                d1 = datetime.strptime(steps[0][1], "%Y-%m-%d")
                d2 = datetime.strptime(steps[-1][1], "%Y-%m-%d")
                total_days = (d2 - d1).days
            except ValueError:
                pass

        legs = legs_score(actions, origin)
        # Extract current committee code from status text like "(S) FIN".
        cmte_match = re.match(r'\([HS]\)\s*(\S+)', status)
        current_code = cmte_match.group(1) if cmte_match else ""
        velocity.append({
            "billnumber": bn, "title": title, "origin": origin, "status": status,
            "committee_code": current_code,
            "referrals": referrals, "steps": step_days, "raw_steps": steps,
            "total_days": total_days, "step_count": len(steps),
            "legs_score": legs["score"], "legs_stage": legs["stage"],
            "legs_stage_label": legs["stage_label"],
        })

    velocity.sort(key=lambda x: (-x["step_count"], x["total_days"] or 9999))

    cutoff_30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    momentum = []
    for v in velocity:
        recent_steps = [s for s in v["raw_steps"] if s[1] >= cutoff_30]
        if not recent_steps:
            continue
        most_recent = max(s[1] for s in recent_steps)
        momentum.append({
            "billnumber": v["billnumber"], "title": v["title"], "origin": v["origin"],
            "status": v["status"], "referrals": v["referrals"],
            "committee_code": v["committee_code"],
            "recent_count": len(recent_steps),
            "most_recent": format_status_date(most_recent),
            "most_recent_raw": most_recent,
            "recent_steps": [{"label": s[0], "date": format_status_date(s[1])} for s in recent_steps],
            "total_steps": v["step_count"],
        })
    momentum.sort(key=lambda x: -x["recent_count"])

    weighted = []
    for v in velocity:
        events = []
        floor_date = None
        for label, raw_date in v["raw_steps"]:
            if label == "Introduced":
                events.append(("Introduced", raw_date))
            elif label.startswith("Rpt:"):
                events.append((label, raw_date))
            elif label in ("2nd Reading", "3rd Reading", "1st Body Passage"):
                if floor_date is None:
                    floor_date = raw_date
                    events.append(("Floor Passage", raw_date))
        if len(events) < 2:
            continue
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
            event_days.append({"label": label, "date": format_status_date(raw_date), "inc_days": inc})
        recent_events = [e for e in events if e[1] >= cutoff_30 and e[0] != "Introduced"]
        most_recent_raw = max((e[1] for e in events), default="")
        total_days = None
        if len(events) > 1:
            try:
                d1 = datetime.strptime(events[0][1], "%Y-%m-%d")
                d2 = datetime.strptime(events[-1][1], "%Y-%m-%d")
                total_days = (d2 - d1).days
            except ValueError:
                pass
        weighted.append({
            "billnumber": v["billnumber"], "title": v["title"], "origin": v["origin"],
            "referrals": v["referrals"], "events": event_days,
            "committee_code": v["committee_code"],
            "event_count": len(events), "recent_count": len(recent_events),
            "most_recent": format_status_date(most_recent_raw),
            "most_recent_raw": most_recent_raw, "total_days": total_days,
        })
    weighted.sort(key=lambda x: -x["recent_count"])

    concur_actions.sort(key=lambda x: x["date"], reverse=True)

    # Legs Score distribution across ALL HB/SB bills (not just top 50).
    legs_buckets = {
        "100 (Chaptered)": 0,
        "80-99": 0,
        "60-79": 0,
        "40-59": 0,
        "20-39": 0,
        "0-19": 0,
    }
    for v in velocity:
        s = v["legs_score"]
        if s == 100:
            legs_buckets["100 (Chaptered)"] += 1
        elif s >= 80:
            legs_buckets["80-99"] += 1
        elif s >= 60:
            legs_buckets["60-79"] += 1
        elif s >= 40:
            legs_buckets["40-59"] += 1
        elif s >= 20:
            legs_buckets["20-39"] += 1
        else:
            legs_buckets["0-19"] += 1
    legs_total = sum(legs_buckets.values())
    legs_distribution = [
        {"band": k, "count": v,
         "pct": round(100 * v / legs_total, 1) if legs_total else 0}
        for k, v in legs_buckets.items()
    ]

    # Sort velocity by Legs Score descending so the most-moving bills
    # show at the top by default. (Users can re-sort by clicking headers.)
    velocity_top = sorted(velocity, key=lambda x: -x.get("legs_score", 0))[:100]

    result = {
        # Top 100 HB/SB bills by Legs Score. Sortable in the UI.
        "velocity": velocity_top,
        # Momentum and weighted tables are inherently "top N" — keep them
        # capped so they remain focused on what's actually moving.
        "momentum": momentum[:50],
        "weighted": weighted[:50],
        "concur": concur_actions,
        "legs_distribution": legs_distribution,
        "legs_total": legs_total,
    }
    _cache.put("bill_progress", result)
    return result


# --- Governor bills ---

def governor_bills(session="34"):
    """Find Governor-introduced bills, categorize, and count hearings."""
    import datetime as dt
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor
    from fetch import SCHEDULE_URL

    cached = _cache.get("governor_bills")
    if cached is not None:
        return cached

    all_bills = scan_all_actions(session)
    bills = []
    status_counts = Counter()
    committee_counts = Counter()

    for bn, origin, title, status, actions in all_bills:
        if not any(code == "050" for code, _, _, _ in actions):
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
            if code in LOW_SIGNAL_CODES:
                pass
            elif jdate >= last_action_date:
                last_action_date = jdate
                last_action_text = text
            if code == "002":
                cmte_reports += 1

        status_upper = status.upper()
        loc = current_chamber(status, origin)
        if "CHAPTER" in status_upper:
            category = "Chaptered"
        elif status_upper == "TRANSM TO GOVERNOR":
            category = "At Governor"
        elif "VETO SUSTAINED" in status_upper:
            category = "Veto Sustained"
        elif "VETO OVERRIDDEN" in status_upper:
            category = "Veto Overridden"
        elif "VETOED" in status_upper:
            category = "Vetoed"
        elif "FAILED" in status_upper:
            category = "Failed"
        elif "WITHDRAWN" in status_upper:
            category = "Withdrawn"
        elif "FLD CONCUR" in status_upper or "CONCURRED" in status_upper:
            category = "Concurrence"
        elif origin == "H" and loc == "S":
            category = "Crossed to Senate"
        elif origin == "S" and loc == "H":
            category = "Crossed to House"
        else:
            category = "In Committee"

        status_counts[category] += 1

        if category == "In Committee" or category.startswith("Crossed"):
            m = re.match(r'\(([HS])\)\s*(\S+)', status)
            if m:
                ch, cmte = m.group(1), m.group(2)
                if cmte.replace("&", "").isalpha() and len(cmte) <= 4:
                    committee_counts[f"({ch}) {cmte}"] += 1

        bills.append({
            "billnumber": compact_billnumber(bn),
            "origin": origin,
            "title": truncate(title, 40),
            "status": status,
            "category": category,
            "referrals": referrals,
            "intro_date": format_status_date(intro_date),
            "last_action": format_status_date(last_action_date),
            "last_action_text": truncate(last_action_text, 40),
            "cmte_reports": cmte_reports,
        })

    # Schedule HTML scan for Governor bill hearings, parallel windows.
    bill_hearing_counts = Counter()
    gov_billnumbers = set(b["billnumber"] for b in bills)
    today = dt.date.today()
    start = dt.date(2025, 1, 1)
    windows = []
    cursor = start
    while cursor < today:
        we = min(cursor + dt.timedelta(days=13), today)
        windows.append((cursor, we))
        cursor = we + dt.timedelta(days=1)

    def _fetch_window_bills(args):
        ws, we = args
        s = ws.strftime("%m/%d/%y")
        e = we.strftime("%m/%d/%y")
        days_old = (today - we).days
        cache_key = f"sched_bills_{s}_{e}"
        cache_max_age = 30 * 24 * 3600 if days_old > 14 else 3600
        cached_bills = _cache.get(cache_key, max_age=cache_max_age)
        if cached_bills is not None:
            return cached_bills

        cached_bills = []
        try:
            url = (
                f"{SCHEDULE_URL}?mode=results&type=&com=&"
                f"startDate={s}&endDate={e}&chamber="
            )
            req = urllib.request.Request(url, headers={"User-Agent": "basis-browser/0.1"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            current_cmte = ""
            current_dt = ""
            seen_in_agenda = set()
            for line in html.split("\n"):
                cm = re.search(r'<td colspan="2">\(([HS])\)([^<]+)</td>', line)
                if cm:
                    current_cmte = f"({cm.group(1)}){cm.group(2).strip()}"
                    seen_in_agenda = set()
                    continue
                dm = re.search(
                    r'<td colspan="2">((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+\s+\w+\s+\d+:\d+\s+[AP]M)</td>',
                    line,
                )
                if dm:
                    current_dt = dm.group(1)
                    seen_in_agenda = set()
                    continue
                bm = re.search(r'Bill/Detail/\?Root=([^"]+)"[^>]*>([^<]+)</a>', line)
                if bm and current_cmte and current_dt:
                    bn = " ".join(bm.group(2).strip().split())
                    agenda_key = f"{current_cmte}|{current_dt}|{bn}"
                    if agenda_key not in seen_in_agenda:
                        seen_in_agenda.add(agenda_key)
                        cached_bills.append(bn)
        except Exception:
            pass
        _cache.put(cache_key, cached_bills)
        return cached_bills

    with ThreadPoolExecutor(max_workers=6) as ex:
        for window_bills in ex.map(_fetch_window_bills, windows):
            for bn in window_bills:
                if bn in gov_billnumbers:
                    bill_hearing_counts[bn] += 1

    for b in bills:
        b["hearings"] = bill_hearing_counts.get(b["billnumber"], 0)

    result = {
        "bills": bills,
        "total": len(bills),
        "status_counts": dict(status_counts),
        "committee_counts": committee_counts.most_common(15),
        "total_hearings": sum(bill_hearing_counts.values()),
        "bills_with_hearings": sum(1 for b in bills if b["hearings"] > 0),
        "total_moveouts": sum(b["cmte_reports"] for b in bills),
        "bills_with_moveouts": sum(1 for b in bills if b["cmte_reports"] > 0),
    }
    _cache.put("governor_bills", result)
    return result


# --- Activity feed ---

STAGE_LABELS = [
    "Introduced",      # 0 — bill exists but no committee referral acted on
    "In Committee",    # 1 — referred (003) but no committee report yet
    "Reported Out",    # 2 — at least one committee report (002)
    "Engaged",         # 3 — committee substitute adopted (009 / 122)
    "Crossed Over",    # 4 — transmitted to other chamber (022)
    "Chaptered",       # 5 — became law (034 / 036)
]


def legs_score(actions, origin, today=None):
    """Compute a 0-100 'has legs' score and a stage 0-5 from a bill's
    full action history. Returns dict with score, stage, and reasons.

    Components were calibrated against session 34 outcomes:
      - 0% of stalled bills had a CS adopted; 64% of crossed bills did,
        and 75% of chaptered bills did. (CS = strongest single signal.)
      - 11% of stalled bills got a committee report within 30 days of
        intro; 33% of crossed; 100% of chaptered.
    """
    if today is None:
        today = datetime.now().date()
    elif isinstance(today, datetime):
        today = today.date()

    intro_date = None
    first_report_date = None
    report_count = 0
    has_cs = False
    crossed = False
    chaptered = False
    last_meaningful = None

    meaningful_codes = {
        "002", "003", "008", "009", "011", "012", "015", "016", "020",
        "022", "026", "029", "032", "033", "034", "036", "122",
    }

    for code, achamber, jdate, text in actions:
        if code == "001" and not intro_date:
            intro_date = jdate
        if code == "002" and achamber == origin:
            report_count += 1
            if not first_report_date:
                first_report_date = jdate
        if code in ("009", "122"):
            has_cs = True
        if code == "022" and achamber == origin:
            crossed = True
        if code in ("034", "036") and "CHAPTER" in text.upper():
            chaptered = True
        if code in meaningful_codes:
            if last_meaningful is None or jdate > last_meaningful:
                last_meaningful = jdate

    # Stage (highest reached). Stages are cumulative milestones.
    if chaptered:
        stage = 5
    elif crossed:
        stage = 4
    elif has_cs:
        stage = 3
    elif report_count >= 1:
        stage = 2
    elif any(c == "003" for c, _, _, _ in actions):
        stage = 1
    else:
        stage = 0

    # Score components.
    reasons = []
    if chaptered:
        return {
            "score": 100,
            "stage": 5,
            "stage_label": STAGE_LABELS[5],
            "reasons": ["Signed into law"],
        }

    score = 0
    # CS adopted (+30)
    if has_cs:
        score += 30
        reasons.append("CS adopted (+30)")

    # Committee report within 30 days of intro (+25)
    if intro_date and first_report_date:
        try:
            d1 = datetime.strptime(intro_date, "%Y-%m-%d").date()
            d2 = datetime.strptime(first_report_date, "%Y-%m-%d").date()
            days_to_report = (d2 - d1).days
            if days_to_report <= 30:
                score += 25
                reasons.append(f"Reported in {days_to_report}d (+25)")
        except ValueError:
            pass

    # Multiple committee reports (+15)
    if report_count >= 2:
        score += 15
        reasons.append(f"{report_count} committee reports (+15)")

    # Recent action in last 14 days (+15)
    if last_meaningful:
        try:
            d = datetime.strptime(last_meaningful, "%Y-%m-%d").date()
            days_since = (today - d).days
            if 0 <= days_since <= 14:
                score += 15
                reasons.append(f"Active in last {days_since}d (+15)")
        except ValueError:
            pass

    # Crossed over (+15)
    if crossed:
        score += 15
        reasons.append("Crossed over (+15)")

    return {
        "score": min(score, 99),  # cap below 100; only chaptered hits 100
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "reasons": reasons,
    }


# Alaska Legislature membership — used for veto-override math.
# House: 40 seats. Senate: 20 seats. Total: 60.
# Standard override (Article II §16): 2/3 of joint = 40 votes.
# Appropriations or revenue: 3/4 of joint = 45 votes.
AK_SEATS_HOUSE = 40
AK_SEATS_SENATE = 20
AK_OVERRIDE_THRESHOLD_STANDARD = 40
AK_OVERRIDE_THRESHOLD_APPROPS = 45


# Token parser for passage-action text like "PASSED Y32 N8 E1".
# Captures (Y/N/E/A) and the number ("-" means zero).
_VOTE_TOKEN_RE = re.compile(r'\b([YNAE])(\d+|-)', re.IGNORECASE)


def parse_vote(text):
    """Parse a passage line into {yeas, nays, excused, absent}.
    Returns None if no Y/N tokens are present."""
    if not text:
        return None
    found = _VOTE_TOKEN_RE.findall(text)
    if not found:
        return None
    out = {"yeas": 0, "nays": 0, "excused": 0, "absent": 0}
    key_map = {"Y": "yeas", "N": "nays", "E": "excused", "A": "absent"}
    saw_y_or_n = False
    for letter, num in found:
        n = 0 if num == "-" else int(num)
        k = key_map[letter.upper()]
        out[k] = n
        if letter.upper() in ("Y", "N"):
            saw_y_or_n = True
    return out if saw_y_or_n else None


# Fiscal-note attachment text: "FN1: ZERO(CED)", "FN2: (DOR)", etc.
_FN_RE = re.compile(r'^(FN\d+):\s*(.*)$')


def categorize_fiscal_note(text):
    """Return 'zero', 'indeterminate', or 'amount' for a fiscal note
    body (the part after 'FNn: '). Used in summarizing whether a bill
    costs money."""
    if not text:
        return "amount"
    u = text.upper()
    if "ZERO" in u:
        return "zero"
    if "INDETERMINATE" in u:
        return "indeterminate"
    return "amount"


# Action codes that are pure bookkeeping (sponsor edits, fiscal-note
# attachments, version registrations, vote rosters). These should NEVER
# win the "Last Action" slot — users expect that column to describe
# the bill's legislative state, not its metadata changes. When multiple
# actions land on the same date, skip these and prefer the procedural one.
LOW_SIGNAL_CODES = {
    "063",  # DP/NR/AM vote roster
    "084",  # Sponsor change
    "086",  # Sponsor change
    "092",  # Cosponsor added
    "100",  # Cross-sponsor added
    "103",  # Version registered
    "105",  # Fiscal note attached
}


def pick_last_action(actions):
    """Return (last_date, last_text) for the most recent *meaningful*
    action in the list. Among same-date actions, the last procedural
    one wins. Falls back to any latest action if every action is
    low-signal (defensive — shouldn't happen on real bills)."""
    last_date = ""
    last_text = ""
    for c, a, jd, t in actions:
        if c in LOW_SIGNAL_CODES:
            continue
        if jd >= last_date:
            last_date = jd
            last_text = t
    if not last_date and actions:
        # Pathological fallback: every action was low-signal.
        latest = max(a[2] for a in actions)
        latest_text = next(a[3] for a in actions if a[2] == latest)
        return latest, latest_text
    return last_date, last_text


NOTABLE_CODES = {
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
    cache_key = f"activity_feed_{days}"
    cached = _cache.get(cache_key, max_age=300)
    if cached is not None:
        return cached

    all_bills = scan_all_actions(session)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    events = []
    for bn, origin, title, status, actions in all_bills:
        prefix = bn.split()[0] if bn else ""
        if prefix not in ("HB", "SB"):
            continue
        for code, achamber, jdate, text in actions:
            if code not in NOTABLE_CODES:
                continue
            if jdate < cutoff:
                continue
            events.append({
                "billnumber": bn, "title": title, "origin": origin,
                "action_chamber": achamber, "date": jdate,
                "date_display": format_status_date(jdate),
                "code": code, "label": NOTABLE_CODES[code], "text": text,
            })
    events.sort(key=lambda x: (x["date"], x["code"]), reverse=True)
    grouped = {}
    for e in events:
        grouped.setdefault(e["date_display"], []).append(e)

    result = {"events": events, "grouped": grouped, "days": days, "total": len(events)}
    _cache.put(cache_key, result)
    return result


# --- Committee detail ---

def committee_detail(chamber, code, session="34"):
    key = f"cmte_detail_{chamber}_{code}"
    cached = _cache.get(key, max_age=600)
    if cached is not None:
        return cached

    bills_here = []
    marker = f"({chamber}) {code}"
    for bn, origin, title, status, actions in scan_all_actions(session):
        if marker not in status:
            continue
        referrals = ""
        for c, a, d, t in actions:
            if c == "091" and a == origin and not referrals:
                referrals = t
        bills_here.append({
            "billnumber": compact_billnumber(bn),
            "title": truncate(title, 50),
            "status": status,
            "referrals": referrals,
        })
    bills_here.sort(key=lambda b: b["billnumber"])

    result = {"chamber": chamber, "code": code, "bills": bills_here}
    _cache.put(key, result)
    return result


# --- Top subjects ---

def top_subjects(session="34", limit=10):
    cached = _cache.get("top_subjects", max_age=3600)
    if cached is not None:
        return cached

    counts = Counter()
    for chamber in ["H", "S"]:
        for b in fetch_all_bills(chamber, session, queries=["Subjects"]):
            for s in b.get("subjects", []):
                counts[s] += 1
    result = counts.most_common(limit)
    _cache.put("top_subjects", result)
    return result


# --- Search ---

def search_bills(query, session="34", limit=50):
    """Search bills by number, title, sponsor name, or subject."""
    q = (query or "").strip().lower()
    if not q:
        return []

    matches = []
    for chamber in ["H", "S"]:
        for b in fetch_all_bills(chamber, session,
                                  queries=["Sponsors", "Versions", "Subjects"]):
            score = 0
            bn = b["billnumber"].lower()
            title = (b.get("short_title") or "").lower()
            sponsor = (b.get("prime_sponsor") or "").lower()
            subjects = [s.lower() for s in b.get("subjects", [])]
            if bn == q:
                score = 100
            elif bn.startswith(q):
                score = 80
            elif q in bn:
                score = 60
            elif q in sponsor:
                score = 40
            elif q in title:
                score = 30
            elif any(q in s for s in subjects):
                score = 20
            if score > 0:
                matches.append({
                    "billnumber": b["billnumber"],
                    "title": b["short_title"],
                    "status": b["status"],
                    "committee_code": b["committee_code"],
                    "prime_sponsor": b.get("prime_sponsor", ""),
                    "subjects": b.get("subjects", []),
                    "score": score,
                })
    matches.sort(key=lambda m: (-m["score"], m["billnumber"]))
    return matches[:limit]


# --- Session countdown ---

# 2nd regular session of the 34th Legislature convened the third Tuesday
# of January 2026; the constitutional 121-day limit puts adjournment
# around 5/20/2026. The legislature can pass a one-time 10-day extension.
SESSION_START = datetime(2026, 1, 20)
SESSION_LIMIT_DAYS = 121

# Article II, Sec 17 of the Alaska Constitution: the governor has
#   15 days to act on bills transmitted DURING session, and
#   20 days to act on bills transmitted AFTER adjournment.
# Sundays are excluded from the count in both windows, but we don't
# model that here — call it a near-miss.
GOVERNOR_DEADLINE_IN_SESSION = 15
GOVERNOR_DEADLINE_POST_SESSION = 20


def governor_deadline_days(transmit_date=None):
    """Return the gubernatorial action window in days. 15 if the
    bill was transmitted while the regular session was still active,
    20 if transmitted after adjournment. If transmit_date is None,
    base the call on today's date — useful for the awaiting-transmittal
    page where we want to show the window that WOULD apply if the
    bill were transmitted right now."""
    if transmit_date is None:
        today = datetime.now().date()
    elif isinstance(transmit_date, str):
        try:
            today = datetime.strptime(transmit_date, "%Y-%m-%d").date()
        except ValueError:
            today = datetime.now().date()
    elif isinstance(transmit_date, datetime):
        today = transmit_date.date()
    else:
        today = transmit_date
    adjournment = (SESSION_START.date()
                   + timedelta(days=SESSION_LIMIT_DAYS - 1))
    return (GOVERNOR_DEADLINE_IN_SESSION if today <= adjournment
            else GOVERNOR_DEADLINE_POST_SESSION)


# Backward-compat alias for callers expecting the old constant.
# Resolves to the deadline that applies *today*; callers that know
# the actual transmittal date should call governor_deadline_days()
# directly with that date.
GOVERNOR_DEADLINE_DAYS = governor_deadline_days()


def session_countdown(today=None):
    """Return days remaining in the regular session and metadata."""
    if today is None:
        today = datetime.now().date()
    elif isinstance(today, datetime):
        today = today.date()
    start = SESSION_START.date()
    day_of_session = (today - start).days + 1
    remaining = SESSION_LIMIT_DAYS - day_of_session
    return {
        "day": max(day_of_session, 0),
        "total": SESSION_LIMIT_DAYS,
        "remaining": max(remaining, 0),
        "adjournment_date": (start + timedelta(days=SESSION_LIMIT_DAYS - 1)).strftime("%b %-d, %Y"),
        "expired": remaining < 0,
    }


# --- End-of-session pipeline ---

# Pipeline stage definitions. Each bill maps to exactly one stage based
# on the most-advanced position it currently sits at.
PIPELINE_STAGES = [
    "Origin Committee",
    "Origin Floor",
    "Crossed Over",
    "Other-Chamber Committee",
    "Other-Chamber Floor",
    "Concurrence",
    "Conference Committee",
    "Awaiting Transmittal",
    "At Governor",
    "Done",
]


def _classify_pipeline_stage(status, actions, origin):
    """Return one of PIPELINE_STAGES based on current position."""
    su = (status or "").upper()
    other = "S" if origin == "H" else "H"

    # Terminal states
    if "CHAPTER" in su or "VETOED" in su or "VETO SUSTAINED" in su \
            or "VETO OVERRIDDEN" in su or "WITHDRAWN" in su \
            or "PERMANENTLY FILED" in su or "FAILED" in su:
        return "Done"

    if "TRANSM TO GOVERNOR" in su or "TRANSMITTED TO GOVERNOR" in su:
        return "At Governor"

    # Passed both chambers, returning to origin chamber for engrossment /
    # transmittal to the governor. Status looks like "RTN TO (S) GOV NEXT".
    if "GOV NEXT" in su or "RTN TO" in su:
        return "Awaiting Transmittal"

    # Conference committee: has 029 but not 032/058 yet
    codes = set(c[0] for c in actions)
    if ("029" in codes or "069" in codes) and not (
        "032" in codes or "058" in codes or "070" in codes
    ):
        return "Conference Committee"

    # Concurrence: bill is in the FLD CONCUR or CONCURRED state, or has
    # been transmitted as amended back to origin (083) without final
    # resolution yet.
    if "FLD CONCUR" in su or "CONCUR MESSAGE" in su:
        return "Concurrence"
    if "022" in codes and "083" in codes and "032" not in codes and "058" not in codes:
        # Transmitted both directions: amendments are being negotiated
        pass  # handled below

    # Crossed over / in other chamber
    current = current_chamber(status, origin)
    if current == other:
        # Currently in other chamber. Floor? Committee?
        if "CAL" in su or "RDG" in su or "FLOOR" in su:
            return "Other-Chamber Floor"
        if "TRANSMITTED" in su or "READ FIRST TIME" in su:
            return "Crossed Over"
        return "Other-Chamber Committee"

    # In origin chamber
    if current == origin:
        if "CAL" in su or "RDG" in su or "PASSD" in su \
                or "RECON" in su or "FLOOR" in su:
            return "Origin Floor"
        # Default: still in committee
        return "Origin Committee"

    # Fallback
    return "Origin Committee"


def pipeline(session="34", min_legs_score=20):
    """Build the end-of-session pipeline view.

    Returns a dict of {stage_label: [bill_summary, ...]} for bills with
    Legs Score >= min_legs_score (so we focus on bills with a real shot
    of moving rather than the long tail of dormant ones).
    """
    cache_key = f"pipeline_v3_{min_legs_score}"
    cached = _cache.get(cache_key, max_age=300)
    if cached is not None:
        return cached

    by_stage = {s: [] for s in PIPELINE_STAGES}
    governor_bills_list = []  # bills currently with governor, with days info

    today = datetime.now().date()

    for bn, origin, title, status, actions in scan_all_actions(session):
        prefix = bn.split()[0] if bn else ""
        if prefix not in ("HB", "SB"):
            continue

        legs = legs_score(actions, origin)
        if legs["score"] < min_legs_score:
            continue

        stage = _classify_pipeline_stage(status, actions, origin)

        # Most recent meaningful date
        last_date = ""
        for c, a, jd, _ in actions:
            if jd >= last_date:
                last_date = jd

        entry = {
            "billnumber": compact_billnumber(bn),
            "title": truncate(title, 50),
            "origin": origin,
            "status": status,
            "legs_score": legs["score"],
            "legs_stage_label": legs["stage_label"],
            "last_date": format_status_date(last_date),
        }

        # Special: for "At Governor", compute the deadline countdown
        if stage == "At Governor":
            # Find the 033 transmittal date
            transmit_date = None
            for c, a, jd, t in actions:
                if c == "033":
                    transmit_date = jd
                    break
            days_at_gov = None
            days_left = None
            deadline_days = None
            if transmit_date:
                try:
                    d = datetime.strptime(transmit_date, "%Y-%m-%d").date()
                    days_at_gov = (today - d).days
                    deadline_days = governor_deadline_days(d)
                    days_left = deadline_days - days_at_gov
                except ValueError:
                    pass
            entry["days_at_governor"] = days_at_gov
            entry["governor_days_left"] = days_left
            entry["governor_deadline_days"] = deadline_days
            governor_bills_list.append(entry)

        by_stage[stage].append(entry)

    # Sort each stage by Legs Score (highest momentum first)
    for stage in by_stage:
        by_stage[stage].sort(key=lambda b: -b["legs_score"])

    # Stage counts for summary
    counts = {s: len(by_stage[s]) for s in PIPELINE_STAGES}

    # Concurrence detail: what's the question?
    concurrence_bills = []
    for bn, origin, title, status, actions in scan_all_actions(session):
        prefix = bn.split()[0] if bn else ""
        if prefix not in ("HB", "SB"):
            continue
        su = (status or "").upper()
        if "FLD CONCUR" not in su and "CONCUR MESSAGE" not in su:
            continue
        # Find the most recent meaningful action (skips bookkeeping)
        last_date, last_text = pick_last_action(actions)
        concurrence_bills.append({
            "billnumber": compact_billnumber(bn),
            "title": truncate(title, 50),
            "origin": origin,
            "status": status,
            "last_date": format_status_date(last_date),
            "last_action_text": last_text,
        })

    # Conference committee detail
    conference_bills = []
    for bn, origin, title, status, actions in scan_all_actions(session):
        prefix = bn.split()[0] if bn else ""
        if prefix not in ("HB", "SB"):
            continue
        codes = set(c[0] for c in actions)
        if "029" not in codes and "069" not in codes:
            continue
        # Pick most recent conference-related action
        last_cc_date = ""
        last_cc_text = ""
        for c, a, jd, t in actions:
            if c in ("029", "030", "031", "032", "058", "059", "067", "069", "070", "079", "109"):
                if jd >= last_cc_date:
                    last_cc_date = jd
                    last_cc_text = t
        # Conferees from action code 030
        conferees = ""
        for c, a, jd, t in actions:
            if c == "030":
                conferees = t  # last one wins
        # Done if conference report adopted in both chambers (032 + 058)
        done = "032" in codes or "058" in codes or "070" in codes
        conference_bills.append({
            "billnumber": compact_billnumber(bn),
            "title": truncate(title, 60),
            "origin": origin,
            "status": status,
            "last_cc_date": format_status_date(last_cc_date),
            "last_cc_text": last_cc_text,
            "conferees": conferees,
            "done": done,
        })
    conference_bills.sort(key=lambda b: (b["done"], b["last_cc_date"]), reverse=True)

    result = {
        "by_stage": by_stage,
        "counts": counts,
        "stages": PIPELINE_STAGES,
        "governor_bills": governor_bills_list,
        "concurrence_bills": concurrence_bills,
        "conference_bills": conference_bills,
        "session": session_countdown(),
    }
    _cache.put(cache_key, result)
    return result


# --- Roll-call helpers shared by awaiting_transmittal + bill_decision_detail ---

def _latest_floor_passage_per_chamber(votes_list, members):
    """Pick the latest 'Third Reading / Final Passage' roll call per
    chamber from a votes list (as returned by fetch_bill_votes). Joins
    each row against the members table to attach name/party/district.

    Returns {'H': [voter, ...], 'S': [voter, ...]} where each voter dict
    has vote/name/party/district/majority. Excludes the 'Advance from
    Second to Third Reading' procedural roll.
    """
    # Bucket by (chamber, date, title). A chamber can hold multiple
    # roll calls on the same day (Third Reading + reconsideration +
    # final passage on an omnibus, e.g. the operating budget). Each
    # roll call is a full 60-member tally; if we merge them by date
    # alone we double- or triple-count yeas. The title disambiguates.
    by_ch_key = {}
    for v in votes_list or []:
        title_u = (v.get("title") or "").upper()
        if "FINAL PASSAGE" not in title_u and "THIRD READING" not in title_u:
            continue
        if "ADVANCE FROM SECOND" in title_u:
            continue
        m = members.get(v.get("member_code") or "", {})
        ch = m.get("chamber", "")
        if ch not in ("H", "S"):
            continue
        key = (ch, v.get("date") or "", title_u)
        by_ch_key.setdefault(key, []).append({
            "vote": v.get("vote", ""),
            "name": m.get("name") or v.get("member_code", ""),
            "party": m.get("party", ""),
            "district": m.get("district", ""),
            "majority": m.get("majority", False),
        })
    out = {"H": [], "S": []}
    dates = {"H": "", "S": ""}
    for ch in ("H", "S"):
        # Candidates: keys for this chamber, sorted by date then title.
        # Pick the latest date; within that date prefer titles that
        # contain "FINAL PASSAGE" over generic "THIRD READING".
        ch_keys = [k for k in by_ch_key if k[0] == ch]
        if not ch_keys:
            continue
        ch_keys.sort(key=lambda k: (
            k[1],                                  # date
            1 if "FINAL PASSAGE" in k[2] else 0,   # prefer final passage
        ))
        chosen = ch_keys[-1]
        dates[ch] = chosen[1]
        # De-dup legislators in case BASIS replays the same row twice.
        seen = set()
        rows = []
        for r in by_ch_key[chosen]:
            if r["name"] in seen:
                continue
            seen.add(r["name"])
            rows.append(r)
        out[ch] = rows
    return out, dates


def _vote_party_breakdown(voter_rows):
    """Collapse per-legislator rows into {'Y': {'D': N, 'R': N, ...},
    'N': {...}} suitable for showing 'Y32 (28D 4R)' in vote chips."""
    out = {"Y": {}, "N": {}, "E": {}, "A": {}}
    for v in voter_rows:
        bucket = out.get(v["vote"])
        if bucket is None:
            continue
        party = v.get("party") or "?"
        bucket[party] = bucket.get(party, 0) + 1
    return out


# --- Awaiting transmittal (passed both chambers, not yet transmitted) ---

def awaiting_transmittal(session="34"):
    """Everything with status 'RTN TO (X) GOV NEXT' — passed both
    chambers, sitting with the origin chamber's secretary awaiting
    engrossment + transmittal. Split into three buckets so bills and
    resolutions can be read separately:

    - bills: HB/SB (the only things the governor actually signs)
    - resolutions_substantive: HJR/SJR (constitutional amendments,
      memorials to Congress) plus HCR/SCR on real subjects
    - resolutions_procedural: HCR/SCR that exist only to suspend or
      amend the chambers' Uniform Rules — pure plumbing

    The gubernatorial clock (15 days during session, 20 days after
    adjournment, per Article II §17) does not start until transmittal,
    so this is the bucket of "passed legislation in suspended animation."
    """
    cached = _cache.get("awaiting_transmittal_v29", max_age=300)
    if cached is not None:
        return cached

    # Enrichment: pull sponsor + subjects from the extended bill feed.
    # fetch_all_bills() is already cached for 10 min so the cost here
    # is one cache hit per chamber on warm requests.
    meta = {}
    for chamber in ("H", "S"):
        for b in fetch_all_bills(chamber, session,
                                  queries=["Sponsors", "Subjects", "Versions"]):
            meta[b["billnumber"]] = b

    members = fetch_members(session)
    # Non-blocking probe of the votes index: if it's been built (warm),
    # we enrich vote chips with party breakdowns; if not, the page still
    # loads with totals-only and breakdowns appear after the index warms.
    votes_idx = _cache.get("all_votes_v2_" + session, max_age=3600) or {}

    # Blue-sheet index: agency analyses dropped in blue_sheets/.
    # Applies to HB/SB only — resolutions don't get blue sheets.
    import blue_sheets as _bs
    _bluesheet_index = _bs.index()
    # Legal-analyses index: LLS / DOL constitutional analyses dropped
    # in legal_analyses/. Applies to both bills AND resolutions.
    import legal_analyses as _la
    _legal_index = _la.index()

    today = datetime.now().date()

    # AWAITING: passed, secretary's office, clock NOT started
    bills = []
    resolutions_substantive = []
    resolutions_procedural = []
    # AT GOVERNOR: transmitted, 15/20-day clock IS running.
    # Bills only — resolutions aren't vetoed even when transmitted,
    # so they go to the resolutions buckets above regardless of state.
    at_gov_bills = []

    # Build a lookup from billnumber → (origin, title, status, actions)
    actions_by_bill = {}
    for bn, origin, title, status, actions in scan_all_actions(session):
        actions_by_bill[bn] = (origin, title, status, actions)

    # SOURCE A: the legislature's "Bills Passed Both Bodies" list.
    # Authoritative for membership AND status text — but occasionally
    # lags by a day or so (HB 16 had a 049 action on May 20 yet still
    # wasn't on the Passed list a week later).
    akleg_passed = {b["billnumber"]: b for b in fetch_passed_bills(session)}

    # SOURCE B: every bill in BASIS whose action history shows it has
    # been transmitted (033) or is awaiting transmittal (049). This
    # catches bills akleg hasn't yet indexed on the Passed list.
    basis_passed = set()
    for bn, _origin, _title, _status, actions in scan_all_actions(session):
        for c, _a, _jd, _t in actions:
            if c in ("033", "049"):
                basis_passed.add(bn)
                break

    # UNION: every billnumber that appears in either source. No bill
    # with a legitimate transmittal signal will be silently dropped.
    all_passed_bns = set(akleg_passed.keys()) | basis_passed

    for bn in all_passed_bns:
        entry_in = akleg_passed.get(bn) or {}
        prefix = bn.split()[0] if bn else ""
        # Accept any prefix that appears in either source — HR/SR
        # single-chamber resolutions included.
        if not prefix:
            continue
        # Status text: prefer akleg's (authoritative when present) but
        # fall back to BASIS status when akleg doesn't have the bill yet.
        if entry_in.get("status"):
            su = entry_in["status"].upper()
        else:
            actions_tuple = actions_by_bill.get(bn)
            su = (actions_tuple[2] if actions_tuple else "").upper()

        # Terminal: chaptered, vetoed, veto sustained, or already
        # filed as a legislative/chamber resolve. These have run
        # their course — drop from the active-decision view.
        if ("CHAPTER" in su or "VETOED" in su or "VETO SUSTAINED" in su
                or "LEGIS RESOLVE" in su or "HOUSE RESOLVE" in su
                or "SENATE RESOLVE" in su):
            continue

        # Pull the bill's action history for both classification AND
        # enrichment. Action codes are authoritative for state; status
        # text from akleg is supplementary.
        actions_tuple = actions_by_bill.get(bn)
        if actions_tuple:
            origin, basis_title, basis_status, actions = actions_tuple
        else:
            origin = "H" if prefix.startswith("H") else "S"
            basis_title = ""
            basis_status = ""
            actions = []
        title = entry_in.get("title") or basis_title
        status = entry_in.get("status") or basis_status

        # Terminal check via action codes (most reliable when 034/036/
        # 038 has fired). Belt-and-suspenders with the status-text
        # check above — both catch slightly different lag patterns.
        latest_terminal = max(
            (jd for c, _, jd, _ in actions if c in ("034", "036", "038")),
            default="",
        )
        latest_033 = max(
            (jd for c, _, jd, _ in actions if c == "033"),
            default="",
        )
        latest_049 = max(
            (jd for c, _, jd, _ in actions if c == "049"),
            default="",
        )
        if latest_terminal and latest_terminal >= max(latest_033, latest_049):
            continue

        # AT GOVERNOR: most recent transmittal-related action is 033
        # AND no later 049 reset it (which would mean returned for
        # additional engrossment — unusual).
        is_at_gov = bool(latest_033) and latest_033 >= latest_049
        # AWAITING TRANSMITTAL: 049 is latest, OR akleg's status text
        # indicates awaiting (catches bills without action data yet).
        is_awaiting = bool(latest_049) and not is_at_gov
        if not (is_at_gov or is_awaiting):
            # Fall back to status-text classification for bills with
            # no relevant action history (very rare).
            is_at_gov = (
                "TRANSM TO GOVERNOR" in su
                or "TRANSMITTED TO GOVERNOR" in su
                or "DUE BACK FROM GOVERNOR" in su
            )
            is_awaiting = (
                not is_at_gov and (
                    "GOV NEXT" in su or "RTN TO" in su
                    or "AWAIT TRANSMIT" in su
                    or "AWAITING TRANSMITTAL" in su
                    or "CONCURRED" in su
                )
            )
            if not (is_at_gov or is_awaiting):
                continue

        # Most recent meaningful action (drives the days-waiting badge
        # and the human-readable "Last Action" column). Skip pure
        # bookkeeping codes via pick_last_action().
        last_date, last_text = pick_last_action(actions)

        # Passage dates and per-chamber vote tallies: action code 020
        # is the Floor Vote / PASSED action. Iterate chronologically so
        # the LATEST passage per chamber wins (handles recommit+repass).
        passage_dates = []
        house_vote = None
        senate_vote = None
        for c, a, jd, t in actions:
            if c != "020":
                continue
            passage_dates.append(jd)
            v = parse_vote(t)
            if v:
                if a == "H":
                    house_vote = v
                elif a == "S":
                    senate_vote = v
        passage_dates.sort()
        passed_both_date = passage_dates[-1] if passage_dates else ""

        # Fiscal notes (105): "FN1: ZERO(CED)", "FN2: (DOR)", etc.
        # Multiple revisions per FN number — keep the latest.
        fn_latest = {}  # {"FN1": "ZERO(CED)"}
        for c, a, jd, t in actions:
            if c != "105" or not t:
                continue
            m = _FN_RE.match(t.strip())
            if m:
                fn_latest[m.group(1)] = m.group(2).strip()
        # Categorize and summarize
        fn_buckets = {"zero": 0, "indeterminate": 0, "amount": 0}
        for body in fn_latest.values():
            fn_buckets[categorize_fiscal_note(body)] += 1
        fiscal_notes = [
            {"label": k, "body": v} for k, v in sorted(fn_latest.items())
        ]

        # Veto-override math: combined yeas across both chambers vs.
        # the 2/3 (40) and 3/4 (45) thresholds. We don't auto-detect
        # appropriations, but expose both numbers so the UI can flag.
        h_y = (house_vote or {}).get("yeas", 0)
        s_y = (senate_vote or {}).get("yeas", 0)
        combined_yeas = h_y + s_y
        is_appropriations = (title or "").upper().startswith("APPROP:")
        override_threshold = (AK_OVERRIDE_THRESHOLD_APPROPS
                              if is_appropriations
                              else AK_OVERRIDE_THRESHOLD_STANDARD)
        veto_proof = (house_vote is not None
                      and senate_vote is not None
                      and combined_yeas >= override_threshold)

        # "Days since passage" — clock starts the moment the bill was
        # fully passed (second chamber's 020 floor vote), not when the
        # AWAITING-TRANSMITTAL housekeeping line was logged 1-2 days
        # later. This is the honest count of how long the bill has
        # been sitting fully passed without being handed to the
        # governor. The gubernatorial clock (15 during session / 20
        # after adjournment) has NOT started.
        days_since_passage = None
        if passed_both_date:
            try:
                d = datetime.strptime(passed_both_date, "%Y-%m-%d").date()
                days_since_passage = (today - d).days
            except ValueError:
                pass

        m = meta.get(compact_billnumber(bn), {})
        sponsor = m.get("prime_sponsor") or ""
        subjects = m.get("subjects") or []
        # sponsor_count includes the prime sponsor; subtract 1 for the
        # cosponsor tally (clamped at 0).
        cosponsor_count = max((m.get("sponsor_count") or 1) - 1, 0)

        # Prime sponsor party/district lookup (by member code from the
        # Sponsors expansion; fall back to scanning by name).
        prime_code = m.get("prime_sponsor_code") or ""
        prime_member = members.get(prime_code, {})
        sponsor_party = prime_member.get("party") or ""
        sponsor_district = prime_member.get("district") or ""

        # Cosponsor party breakdown: count Ds vs Rs (and others) among
        # the non-prime sponsors. Gives a "bipartisan-cosponsored"
        # signal which makes a veto politically costlier.
        cosponsor_by_party = {}
        for sp in m.get("sponsors") or []:
            if sp.get("prime"):
                continue
            sp_member = members.get(sp.get("code") or "", {})
            sp_party = sp_member.get("party") or "?"
            cosponsor_by_party[sp_party] = cosponsor_by_party.get(sp_party, 0) + 1

        # Per-chamber floor-passage roll calls (only available once the
        # votes index has built). Used to enrich vote chips on the card
        # with party breakdowns.
        compact_bn = compact_billnumber(bn)
        if votes_idx and compact_bn in votes_idx:
            roll_calls, _ = _latest_floor_passage_per_chamber(
                votes_idx[compact_bn], members,
            )
            house_breakdown = _vote_party_breakdown(roll_calls["H"])
            senate_breakdown = _vote_party_breakdown(roll_calls["S"])
            # If we have roll-call data, prefer THAT for the chamber
            # vote totals (action-text parsing can occasionally lose a
            # vote to formatting quirks). Counts derived from the
            # legislator-level list are authoritative.
            if roll_calls["H"]:
                house_vote = {
                    "yeas": sum(house_breakdown["Y"].values()),
                    "nays": sum(house_breakdown["N"].values()),
                    "excused": sum(house_breakdown["E"].values()),
                    "absent": sum(house_breakdown["A"].values()),
                }
            if roll_calls["S"]:
                senate_vote = {
                    "yeas": sum(senate_breakdown["Y"].values()),
                    "nays": sum(senate_breakdown["N"].values()),
                    "excused": sum(senate_breakdown["E"].values()),
                    "absent": sum(senate_breakdown["A"].values()),
                }
            # Recompute combined yeas + veto-proof using authoritative totals.
            h_y = (house_vote or {}).get("yeas", 0)
            s_y = (senate_vote or {}).get("yeas", 0)
            combined_yeas = h_y + s_y
            veto_proof = (house_vote is not None
                          and senate_vote is not None
                          and combined_yeas >= override_threshold)
        else:
            house_breakdown = None
            senate_breakdown = None

        # akleg.gov canonical bill page — single click to text, fiscal
        # notes, sponsor statement, full action history, journal links.
        akleg_url = (
            "https://www.akleg.gov/basis/Bill/Detail/"
            + session + "?Root=" + compact_bn.replace(" ", "%20")
        )

        # At-governor-specific fields: find the transmittal date (033)
        # and the authoritative return-by date (096 — "DUE BACK FROM
        # GOVERNOR mm/dd/yy"). The legislature computes the return
        # date itself, properly excluding Sundays per Article II §17.
        # Always prefer that over our calendar arithmetic.
        transmit_date = ""
        return_by_date_str = ""  # raw mm/dd/yy from action 096
        return_by_date = None    # parsed date object
        days_at_governor = None
        gov_deadline = None
        gov_days_left = None
        if is_at_gov:
            for c, _, jd, t in actions:
                if c == "033" and jd and not transmit_date:
                    transmit_date = jd
                if c == "096" and t and not return_by_date_str:
                    # Text format: "DUE BACK FROM GOVERNOR 5/30/26"
                    m_due = re.search(
                        r"DUE BACK FROM GOVERNOR\s+(\d{1,2})/(\d{1,2})/(\d{2,4})",
                        t, re.IGNORECASE,
                    )
                    if m_due:
                        mm, dd, yy = m_due.groups()
                        year = int(yy)
                        if year < 100:
                            year += 2000
                        try:
                            return_by_date = datetime(year, int(mm), int(dd)).date()
                            return_by_date_str = m_due.group(0).split("GOVERNOR")[-1].strip()
                        except ValueError:
                            pass
            if return_by_date:
                gov_days_left = (return_by_date - today).days
            if transmit_date:
                try:
                    td = datetime.strptime(transmit_date, "%Y-%m-%d").date()
                    days_at_governor = (today - td).days
                    # Window inferred from session timing — still useful
                    # to display "15-day window" or "20-day window".
                    gov_deadline = governor_deadline_days(td)
                    # Fall back to calendar math only if the legislature
                    # hasn't yet logged the 096 due-back action.
                    if gov_days_left is None:
                        gov_days_left = gov_deadline - days_at_governor
                except ValueError:
                    pass
        # Stale at-gov filter: if the gubernatorial deadline lapsed
        # more than a week ago, the bill has almost certainly been
        # disposed (signed, became-law-without-signature, or vetoed)
        # but akleg's status field hasn't been updated. Don't show
        # these — they're noise, not active decisions. Example: SR 4
        # transmitted Feb 13 2026, 87 days past deadline, status
        # still 'TRANSM TO GOVERNOR'.
        if (is_at_gov and gov_days_left is not None
                and gov_days_left < -7):
            continue
        # Stale awaiting filter: anything in the awaiting bucket whose
        # most recent meaningful action is more than 60 days old is
        # almost certainly stuck or the legislature is dead. Drop.
        if is_awaiting and last_date:
            try:
                last_d = datetime.strptime(last_date, "%Y-%m-%d").date()
                if (today - last_d).days > 60:
                    continue
            except ValueError:
                pass

        entry = {
            "billnumber": compact_billnumber(bn),
            "title": truncate(title, 80),
            "origin": origin,
            "status": status,
            "last_date": format_status_date(last_date),
            "last_action_text": last_text,
            "type": prefix,
            "sponsor": sponsor,
            "sponsor_party": sponsor_party,
            "sponsor_district": sponsor_district,
            "cosponsor_count": cosponsor_count,
            "cosponsor_by_party": cosponsor_by_party,
            "subjects": subjects[:5],
            "akleg_url": akleg_url,
            # Blue sheets — list of curated agency analyses on disk.
            # Empty for resolutions (template suppresses the section
            # when type is not HB/SB).
            "blue_sheets": _bluesheet_index.get(compact_billnumber(bn), []),
            # Legal analyses — LLS / DOL constitutional/legal reviews.
            # Applies to both bills and resolutions.
            "legal_analyses": _legal_index.get(compact_billnumber(bn), []),
            "passed_both_date": format_status_date(passed_both_date),
            "days_since_passage": days_since_passage,
            # Vote tallies and veto-override math
            "house_vote": house_vote,
            "senate_vote": senate_vote,
            "house_breakdown": house_breakdown,
            "senate_breakdown": senate_breakdown,
            "combined_yeas": combined_yeas,
            "override_threshold": override_threshold,
            "is_appropriations": is_appropriations,
            "veto_proof": veto_proof,
            # Fiscal + effective-date
            "fiscal_notes": fiscal_notes,
            "fiscal_summary": fn_buckets,
            # At-governor flag + clock fields (populated only when at gov)
            "is_at_governor": is_at_gov,
            "transmit_date": format_status_date_full(transmit_date),
            "days_at_governor": days_at_governor,
            "governor_deadline_days": gov_deadline,
            "governor_days_left": gov_days_left,
            # Authoritative return-by date string from action 096 if
            # available — e.g. "5/30/26". Sourced from the legislature
            # directly, so the Sunday-exclusion math is theirs.
            "return_by_date": (return_by_date.strftime("%b %-d, %Y")
                               if return_by_date else ""),
        }

        # Route to bucket. Resolutions don't get vetoed — even when
        # transmitted to the governor, they're ceremonial/informational
        # (memorials to Congress, constitutional amendment proposals,
        # internal procedural concurrent resolutions). They belong with
        # the other resolutions, not in the urgency-countdown section.
        if prefix in ("HB", "SB"):
            (at_gov_bills if is_at_gov else bills).append(entry)
        elif is_procedural_resolution(bn, title):
            resolutions_procedural.append(entry)
        else:
            resolutions_substantive.append(entry)

    # Bills sorted by days_since_passage desc (longest sitting first —
    # that's the signal you want when scanning for held bills).
    bills.sort(key=lambda b: -(b["days_since_passage"] or 0))
    for lst in (resolutions_substantive, resolutions_procedural):
        lst.sort(key=lambda b: b["last_date"], reverse=True)
    # At-governor buckets sorted by days_left ASC (most urgent first —
    # bills closest to auto-law deadline rise to the top).
    def _atgov_sort_key(b):
        d = b.get("governor_days_left")
        return d if d is not None else 9999
    at_gov_bills.sort(key=_atgov_sort_key)

    result = {
        # Awaiting transmittal — passed, clock NOT started
        "bills": bills,
        "resolutions_substantive": resolutions_substantive,
        "resolutions_procedural": resolutions_procedural,
        # At governor — transmitted, clock IS running (bills only).
        # Empty list emitted for backward compat with the template.
        "at_gov_bills": at_gov_bills,
        "at_gov_resolutions": [],
        "counts": {
            "bills": len(bills),
            "resolutions_substantive": len(resolutions_substantive),
            "resolutions_procedural": len(resolutions_procedural),
            "at_gov_bills": len(at_gov_bills),
            "at_gov_resolutions": 0,
        },
        # Which gov window kicks in if any of these are transmitted
        # today — 15 (in session) or 20 (post-adjournment).
        "gov_deadline_if_transmitted_today": governor_deadline_days(),
    }
    _cache.put("awaiting_transmittal_v29", result)
    return result


# --- Per-bill decision detail (lazy-loaded) ---

def bill_decision_detail(billnumber, session="34"):
    """Detailed veto-decision view for one bill: full per-legislator
    roll call on final passage in each chamber, plus action timeline
    and fiscal-note bodies. Cached 10 min."""
    cache_key = f"bill_decision_detail_v7_{session}_{billnumber}"
    cached = _cache.get(cache_key, max_age=600)
    if cached is not None:
        return cached

    members = fetch_members(session)
    raw_votes = fetch_bill_votes(billnumber, session)

    # Shared helper: pick the latest "Third Reading / Final Passage"
    # roll call per chamber, with members joined for name/party.
    passage_votes, passage_dates = _latest_floor_passage_per_chamber(
        raw_votes, members,
    )
    # Sort each chamber's roster: Y first, then N, then E/A; within
    # each group, by last name.
    order = {"Y": 0, "N": 1, "E": 2, "A": 3}
    for ch in ("H", "S"):
        passage_votes[ch] = sorted(
            passage_votes[ch],
            key=lambda r: (order.get(r["vote"], 9),
                           (r["name"].rsplit(" ", 1)[-1] if r["name"] else "")),
        )

    # Tallies
    counts = {ch: {"Y": 0, "N": 0, "E": 0, "A": 0}
              for ch in ("H", "S")}
    for ch, votes in passage_votes.items():
        for v in votes:
            if v["vote"] in counts[ch]:
                counts[ch][v["vote"]] += 1

    # Cosponsor list + long bill title from the extended bill feed.
    # Versions expansion carries the legal "An Act relating to..."
    # text — much more informative than the short title shown on the
    # card. We grab the latest version's title for the summary.
    cosponsors = []
    long_title = ""
    version_letter = ""
    bill_meta = None
    for chamber in ("H", "S"):
        for b in fetch_all_bills(chamber, session,
                                  queries=["Sponsors", "Subjects", "Versions"]):
            if b.get("billnumber") == billnumber:
                bill_meta = b
                break
        if bill_meta:
            break
    if bill_meta:
        long_title = bill_meta.get("latest_version_title") or ""
        # Strip the boilerplate effective-date clause that ends nearly
        # every Alaska "An Act..." title — adds nothing the user cares
        # about and crowds out the substantive provisions.
        long_title = re.sub(
            r"[;,]?\s*(and\s+)?providing for an effective date\.?\s*$",
            ".",
            long_title,
            flags=re.IGNORECASE,
        ).strip()
        version_letter = bill_meta.get("latest_version_letter") or ""
        for sp in bill_meta.get("sponsors") or []:
            if sp.get("prime"):
                continue
            sp_member = members.get(sp.get("code") or "", {})
            cosponsors.append({
                "code": sp.get("code") or "",
                "name": sp_member.get("name") or sp.get("name") or "",
                "party": sp_member.get("party") or "",
                "district": sp_member.get("district") or "",
                "chamber": sp_member.get("chamber") or "",
            })
        # Stable order: by chamber (H first), then party (D, R), then name
        party_order = {"D": 0, "R": 1}
        cosponsors.sort(key=lambda c: (
            0 if c["chamber"] == "H" else 1,
            party_order.get(c["party"], 9),
            (c["name"].rsplit(" ", 1)[-1] if c["name"] else ""),
        ))

    # Key milestone dates per bill.
    milestones = {
        "introduced": "",
        "first_committee_report": "",
        "first_cs_adopted": "",
        "crossover": "",  # transmitted to other chamber
        "passed_house": "",
        "passed_senate": "",
    }
    actions_for_bill = []
    for bn, origin, title, status, actions in scan_all_actions(session):
        if bn == billnumber:
            actions_for_bill = list(actions)
            break

    # Milestone scan
    for c, a, jd, t in actions_for_bill:
        if c == "001" and not milestones["introduced"]:
            milestones["introduced"] = jd
        elif c == "002" and not milestones["first_committee_report"]:
            milestones["first_committee_report"] = jd
        elif c in ("009", "122") and not milestones["first_cs_adopted"]:
            milestones["first_cs_adopted"] = jd
        elif c == "022" and not milestones["crossover"]:
            # 022 in origin chamber = transmitted to other chamber
            milestones["crossover"] = jd
        elif c == "020" and a == "H" and not milestones["passed_house"]:
            milestones["passed_house"] = jd
        elif c == "020" and a == "S" and not milestones["passed_senate"]:
            milestones["passed_senate"] = jd

    # Days-between calculations for the milestone narrative
    def _days_between(a, b):
        try:
            da = datetime.strptime(a, "%Y-%m-%d").date()
            db = datetime.strptime(b, "%Y-%m-%d").date()
            return (db - da).days
        except (ValueError, TypeError):
            return None

    days_intro_to_first_cmte = _days_between(
        milestones["introduced"], milestones["first_committee_report"]
    )
    days_intro_to_passage = None
    if milestones["passed_house"] and milestones["passed_senate"]:
        last = max(milestones["passed_house"], milestones["passed_senate"])
        days_intro_to_passage = _days_between(milestones["introduced"], last)

    milestones_display = {k: format_status_date_full(v) for k, v in milestones.items()}

    # Fiscal-note bodies (full text, with dates).
    fiscal_notes = []
    for c, a, jd, t in actions_for_bill:
        if c == "105" and t:
            m = _FN_RE.match(t.strip())
            if m:
                fiscal_notes.append({
                    "label": m.group(1),
                    "body": m.group(2).strip(),
                    "date": jd,
                    "category": categorize_fiscal_note(m.group(2)),
                })

    result = {
        "billnumber": billnumber,
        "passage_votes": passage_votes,
        "passage_dates": {k: format_status_date(v)
                          for k, v in passage_dates.items()},
        "vote_counts": counts,
        "cosponsors": cosponsors,
        "long_title": long_title,
        "version_letter": version_letter,
        # Hand-authored neutral summary (None if not yet written for
        # this bill — template falls back to Legal Description only).
        "neutral_summary": get_bill_summary(billnumber, session),
        # Sponsor statement still fetched so the PDF link is available
        # for users who want the advocacy framing. We no longer surface
        # the body text inline since the user wants editorial neutrality.
        "sponsor_statement": fetch_sponsor_statement(billnumber, session),
        "milestones": milestones_display,
        "days_intro_to_first_committee": days_intro_to_first_cmte,
        "days_intro_to_passage": days_intro_to_passage,
        "fiscal_notes": fiscal_notes,
    }
    _cache.put(cache_key, result)
    return result


# --- Cache freshness ---

def cache_freshness():
    """Return dict of cache key -> seconds since cache write."""
    now = time.monotonic()
    return {k: int(now - entry["time"]) for k, entry in _cache._cache.items()}
