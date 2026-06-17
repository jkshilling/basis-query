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


def format_status_date_full(date_str: str) -> str:
    """Like format_status_date but includes year — useful for bills
    that carry over from one regular session to the next, where
    'May 2' alone is ambiguous between two different years."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%b %-d, %Y")
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
            "prime_sponsor_code": "",
            "sponsor_count": 0,
            "sponsors": [],
            "version_count": 0,
            "latest_version_letter": "",
            "latest_version_title": "",
            "subjects": [],
            # Sponsorship extensions (issue #3): committee bills name a
            # Committee + an optional Requestor. The Requestor names the
            # real entity behind a committee bill ("THE GOVERNOR" or a
            # task force / department). The committee identifies which
            # committee chamber + code introduced it (RLS, FIN, etc.).
            "requestor": "",
            "committee_sponsor_code": "",
            "committee_sponsor_name": "",
            "committee_sponsor_chamber": "",
            # Sponsor-statement PDF URL straight from BASIS (a
            # <SponsorStatement> child of <Sponsors>). Saves the
            # expander a full akleg HTML scrape + PDF download.
            "sponsor_statement_url": "",
            # Fiscal-note PDFs (issue #2): the FiscalNotes expansion of
            # the Bills endpoint exposes each FN's preparer agency, fiscal-
            # impact letter (N/P/I/etc.), and a direct PDF URL.
            "fn_pdfs": [],
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
            sponsor_list = []
            for member in sponsors:
                tag = strip_ns(member.tag)
                if tag == "MemberDetails":
                    count += 1
                    code = member.attrib.get("code", "")
                    first = child_text(member, "FirstName")
                    last = child_text(member, "LastName")
                    is_prime = member.attrib.get("primesponsor") == "true"
                    name = (first + " " + last).strip()
                    sponsor_list.append({
                        "code": code,
                        "name": name or code,
                        "prime": is_prime,
                    })
                    if is_prime:
                        bill["prime_sponsor"] = name
                        bill["prime_sponsor_code"] = code
                elif tag == "Committee":
                    if not bill["prime_sponsor"]:
                        bill["prime_sponsor"] = f"({member.attrib.get('code', '')})"
                    bill["committee_sponsor_code"] = member.attrib.get("code", "")
                    bill["committee_sponsor_chamber"] = member.attrib.get("chamber", "")
                    bill["committee_sponsor_name"] = (member.text or "").strip()
                    count += 1
                elif tag == "Requestor":
                    # BASIS uses literal '%' as a placeholder for "no
                    # requestor" — treat that as empty. Whitespace-pad
                    # is common too.
                    req = (member.text or "").strip()
                    if req and req != "%":
                        bill["requestor"] = req
                elif tag == "SponsorStatement":
                    url = (member.text or "").strip()
                    if url:
                        bill["sponsor_statement_url"] = url
            bill["sponsor_count"] = count
            bill["sponsors"] = sponsor_list

        # Fiscal-note PDFs from the FiscalNotes expansion. Each entry
        # exposes the preparer agency + impact code + direct PDF URL.
        for fn_root in elem:
            if strip_ns(fn_root.tag) != "FiscalNotes":
                continue
            for fn in fn_root:
                if strip_ns(fn.tag) != "FiscalNote":
                    continue
                url = ""
                for content in fn:
                    if strip_ns(content.tag) == "Content":
                        for u in content:
                            if strip_ns(u.tag) == "Url":
                                url = (u.text or "").strip()
                                break
                bill["fn_pdfs"].append({
                    "name":     fn.attrib.get("name", "").strip(),
                    "date":     fn.attrib.get("date", "").strip(),
                    "preparer": fn.attrib.get("preparer", "").strip(),
                    "impact":   fn.attrib.get("fiscalimpact", "").strip(),
                    "url":      url,
                })

        for versions in elem:
            if strip_ns(versions.tag) != "Versions":
                continue
            ver_count = 0
            latest_letter = ""
            latest_title = ""
            # For veto letters / formal correspondence we need the
            # canonical pre-engrossment designator (e.g.
            # "HCS CSSB 24(FIN) am H"). The Z (enrolled) version's
            # name is just "Enrolled SB 24"; the operative form is
            # whichever non-Z version has the highest letter.
            operative_letter = ""
            operative_name = ""
            operative_title_quoted = ""
            for ver in versions:
                if strip_ns(ver.tag) == "Version":
                    letter = ver.attrib.get("versionletter", "")
                    name = ver.attrib.get("name", "") or ""
                    if letter != "Z":
                        ver_count += 1
                    # Keep the alphabetically-latest version's title.
                    # Versions are issued A, B, C in adoption order, so
                    # max letter = current operative version.
                    title = child_text(ver, "Title") or ""
                    if letter and letter > latest_letter:
                        latest_letter = letter
                        latest_title = title.strip().strip('"').strip()
                    if letter and letter != "Z" and letter > operative_letter:
                        operative_letter = letter
                        operative_name = name.strip()
                        # Pre-Z titles arrive with a leading quote +
                        # "An Act" prefix; preserve verbatim for formal
                        # correspondence rendering.
                        operative_title_quoted = title.strip()
            bill["version_count"] = ver_count
            bill["latest_version_letter"] = latest_letter
            bill["latest_version_title"] = latest_title
            bill["operative_version_letter"] = operative_letter
            bill["operative_version_name"]   = operative_name
            bill["operative_version_title"]  = operative_title_quoted

        bills.append(bill)
    return bills
