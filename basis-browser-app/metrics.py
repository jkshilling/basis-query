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
    compact_billnumber, current_chamber, format_status_date, truncate,
)
from fetch import (
    fetch_all_bills, fetch_hearing_schedule, fetch_hearing_counts,
    fetch_committee_reports, scan_all_actions, count_actions_by_year,
    fetch_bill_detail,
)


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

    # Floor calendar.
    house_floor = []
    senate_floor = []
    for b in all_bills:
        status = b["status"]
        status_upper = status.upper()
        if "CAL" not in status_upper or "SECY" in status_upper:
            continue
        cal_match = re.search(r'CAL\(([HS])\)', status)
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
            (house_floor if cal_match.group(1) == "H" else senate_floor).append(entry)
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
        velocity.append({
            "billnumber": bn, "title": title, "origin": origin, "status": status,
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
    velocity_full = sorted(velocity, key=lambda x: -x.get("legs_score", 0))

    result = {
        # All HB/SB bills, sorted by Legs Score. Sortable in the UI.
        "velocity": velocity_full,
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
            if jdate > last_action_date:
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


# --- Cache freshness ---

def cache_freshness():
    """Return dict of cache key -> seconds since cache write."""
    now = time.monotonic()
    return {k: int(now - entry["time"]) for k, entry in _cache._cache.items()}
