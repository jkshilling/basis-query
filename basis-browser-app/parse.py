"""Pure data transformation: XML helpers, string formatting, status interpretation.

No I/O. No imports from fetch.py or metrics.py. Safe to call from anywhere.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime


# --- XML helpers ---

def strip_ns(tag: str) -> str:
    """Remove an XML namespace prefix from a tag name."""
    return tag.rsplit("}", 1)[1] if "}" in tag else tag


def child_text(elem, name: str) -> str:
    for child in elem:
        if strip_ns(child.tag) == name:
            return (child.text or "").strip()
    return ""


def child_attr(elem, name: str, attr: str) -> str:
    for child in elem:
        if strip_ns(child.tag) == name:
            return child.attrib.get(attr, "")
    return ""


# --- String formatting ---

def format_status_date(date_str: str) -> str:
    """Turn '2026-04-13' into 'Apr 13'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%b %-d")
    except (ValueError, TypeError):
        return date_str


def compact_billnumber(raw: str) -> str:
    """'HB  173' -> 'HB 173'. Collapse internal whitespace."""
    return " ".join(raw.split())


def truncate(text: str, length: int = 60) -> str:
    if len(text) <= length:
        return text
    return text[:length - 1].rstrip() + "…"


def compact_hearing(raw: str) -> str:
    """'Apr 21 Tuesday 3:30 PM' -> 'Apr 21, Tue, 3:30 PM'."""
    if not raw:
        return raw
    day_map = {
        "Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed",
        "Thursday": "Thu", "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun",
    }
    for full, abbr in day_map.items():
        raw = raw.replace(full, abbr)
    m = re.match(r'(\w+ \d+)\s+(\w+)\s+(.+)', raw)
    if m:
        return f"{m.group(1)}, {m.group(2)}, {m.group(3)}"
    return raw


def parse_hearing_datetime(raw: str, year=None):
    """Parse 'Apr 16 Wednesday 1:30 PM' into a datetime object."""
    if not year:
        year = datetime.now().year
    try:
        parts = raw.split()
        # parts: ['Apr', '16', 'Wednesday', '1:30', 'PM']
        cleaned = f"{parts[0]} {parts[1]} {parts[3]} {parts[4]} {year}"
        return datetime.strptime(cleaned, "%b %d %I:%M %p %Y")
    except (ValueError, IndexError):
        return None


# --- Status interpretation ---

def current_chamber(status: str, origin: str):
    """Determine where a bill currently lives based on its StatusText.

    Returns:
        'H' or 'S' — bill is currently in that chamber
        'GOV'      — at the governor / signed / vetoed
        'DONE'     — terminal (failed, withdrawn, perm filed, etc.)
        None       — unknown / unparseable

    Avoids the trap where '(S)AM' or '(H) AM' inside compound statuses like
    'FLD CONCUR(S)AM' or 'CONCURRED(H) AM' is mistaken for a current location.
    """
    if not status:
        return None
    su = status.upper()

    if "CHAPTER" in su or "SIGNED INTO LAW" in su or "LAW W/O" in su:
        return "GOV"
    if "VETOED" in su or "VETO SUSTAINED" in su or "VETO OVERRIDDEN" in su:
        return "GOV"
    if "TRANSM TO GOVERNOR" in su or "TRANSMITTED TO GOVERNOR" in su:
        return "GOV"
    if "WITHDRAWN" in su or "PERMANENTLY FILED" in su or "LEGIS RESOLVE" in su:
        return "DONE"
    if "FAILED" in su:
        return "DONE"

    # Concurrence outcomes return the bill to its origin chamber.
    if "FLD CONCUR" in su or "CONCURRED" in su:
        return origin

    if su.startswith("(H)"):
        return "H"
    if su.startswith("(S)"):
        return "S"

    if "TRANSMITTED TO (S)" in su:
        return "S"
    if "TRANSMITTED TO (H)" in su:
        return "H"
    if "READ FIRST TIME (S)" in su:
        return "S"
    if "READ FIRST TIME (H)" in su:
        return "H"
    if "CAL(S)" in su or "(S) CALENDAR" in su:
        return "S"
    if "CAL(H)" in su or "(H) CALENDAR" in su:
        return "H"

    return None


# --- Bill XML parsing ---

def next_referral(elem, current_code: str, other_chamber: str) -> str:
    """Find the next committee of referral after current_code in the
    referral list (action code 091) of the given chamber.
    """
    for actions in elem:
        if strip_ns(actions.tag) != "Actions":
            continue
        for action in actions:
            if (action.attrib.get("chamber", "") == other_chamber
                    and action.attrib.get("code", "") == "091"):
                referral_text = child_text(action, "ActionText")
                codes = [c.strip() for c in referral_text.split(",")]
                try:
                    idx = codes.index(current_code)
                    if idx + 1 < len(codes):
                        return codes[idx + 1]
                except ValueError:
                    pass
    return ""


def parse_bills(result, other_chamber: str | None = None):
    """Parse bill XML into a list of plain dicts. Includes next_referral
    when other_chamber is provided."""
    body = result["body"].decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    bills = []
    for elem in root.iter():
        if strip_ns(elem.tag) != "Bill":
            continue
        committee_code = child_attr(elem, "CurrentCommittee", "committeecode")
        next_ref = ""
        if other_chamber:
            next_ref = next_referral(elem, committee_code, other_chamber)
        bills.append({
            "billnumber": compact_billnumber(elem.attrib.get("billnumber", "")),
            "chamber": elem.attrib.get("chamber", "").strip(),
            "short_title": truncate(child_text(elem, "ShortTitle")),
            "status": child_text(elem, "StatusText"),
            "status_code": child_attr(elem, "StatusText", "statuscode"),
            "status_date": format_status_date(child_text(elem, "StatusDate")),
            "committee": child_text(elem, "CurrentCommittee"),
            "committee_code": committee_code,
            "next_referral": next_ref,
        })
    return bills


def parse_bills_extended(result):
    """Parse bills with Sponsors, Versions, and Subjects expansions."""
    body = result["body"].decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    bills = []
    for elem in root.iter():
        if strip_ns(elem.tag) != "Bill":
            continue

        bill = {
            "billnumber": compact_billnumber(elem.attrib.get("billnumber", "")),
            "chamber": elem.attrib.get("chamber", "").strip(),
            "short_title": truncate(child_text(elem, "ShortTitle")),
            "status": child_text(elem, "StatusText"),
            "status_code": child_attr(elem, "StatusText", "statuscode"),
            "status_date": format_status_date(child_text(elem, "StatusDate")),
            "committee": child_text(elem, "CurrentCommittee"),
            "committee_code": child_attr(elem, "CurrentCommittee", "committeecode"),
            "next_referral": "",
            "prime_sponsor": "",
            "sponsor_count": 0,
            "version_count": 0,
            "subjects": [],
        }

        for subs in elem:
            if strip_ns(subs.tag) != "Subjects":
                continue
            for sub in subs:
                if strip_ns(sub.tag) == "Subject":
                    txt = (sub.text or "").strip()
                    if txt:
                        bill["subjects"].append(txt)

        for sponsors in elem:
            if strip_ns(sponsors.tag) != "Sponsors":
                continue
            count = 0
            for member in sponsors:
                if strip_ns(member.tag) == "MemberDetails":
                    count += 1
                    if member.attrib.get("primesponsor") == "true":
                        first = child_text(member, "FirstName")
                        last = child_text(member, "LastName")
                        bill["prime_sponsor"] = f"{first} {last}"
                elif strip_ns(member.tag) == "Committee":
                    if not bill["prime_sponsor"]:
                        bill["prime_sponsor"] = f"({member.attrib.get('code', '')})"
                    count += 1
            bill["sponsor_count"] = count

        for versions in elem:
            if strip_ns(versions.tag) != "Versions":
                continue
            ver_count = 0
            for ver in versions:
                if strip_ns(ver.tag) == "Version":
                    letter = ver.attrib.get("versionletter", "")
                    if letter != "Z":
                        ver_count += 1
            bill["version_count"] = ver_count

        bills.append(bill)
    return bills
