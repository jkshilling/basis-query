#!/usr/bin/env python3
"""
Minimal stdlib-only CLI for the Alaska Legislature BASIS API.

Examples:
  python3 query_basis.py --section bills --session 34
  python3 query_basis.py --section bills --session 34 --query "Actions" --range "..1"
  python3 query_basis.py --section meetings --session 34 --query "Media" --range "..3"
  python3 query_basis.py --section bills --session 34 --head
"""

from __future__ import annotations

import argparse
import gzip
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, List, Tuple
import xml.etree.ElementTree as ET
from xml.dom import minidom
from xml.parsers.expat import ExpatError


DEFAULT_BASE_URL = "https://www.akleg.gov/publicservice/basis"
DEFAULT_VERSION = "1.4"
VALID_SECTIONS = ("bills", "members", "committees", "sessions", "meetings")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query the Alaska Legislature BASIS API with a tiny local CLI."
    )

    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Base URL for BASIS API (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--section",
        required=True,
        choices=VALID_SECTIONS,
        help="Top-level BASIS section to query.",
    )
    parser.add_argument(
        "--path",
        default="",
        help="Optional extra path/subsection after the section, e.g. 'detail' or an identifier.",
    )
    parser.add_argument(
        "--session",
        help="Session number query parameter.",
    )
    parser.add_argument(
        "--chamber",
        help="Chamber query parameter, e.g. H or S.",
    )
    parser.add_argument(
        "--minifyresult",
        choices=("true", "false"),
        help="Optional minifyresult query parameter.",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional query parameter. Repeat as needed.",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        metavar="QUERY",
        help=(
            "Add a repeated X-Alaska-Legislature-Basis-Query header. "
            "Repeat this flag for multiple query headers."
        ),
    )
    parser.add_argument(
        "--range",
        dest="result_range",
        help="Optional X-Alaska-Query-ResultRange header, e.g. '..10' or '1..25'.",
    )
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help=f"BASIS version header value (default: {DEFAULT_VERSION})",
    )
    parser.add_argument(
        "--head",
        action="store_true",
        help="Use HTTP HEAD instead of GET.",
    )
    parser.add_argument(
        "--options",
        action="store_true",
        help="Use HTTP OPTIONS instead of GET.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw response body instead of pretty-printing XML.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional path to save the raw response body.",
    )

    return parser


def parse_extra_params(items: Iterable[str]) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid --param value '{item}'. Expected KEY=VALUE.")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid --param value '{item}'. Key cannot be empty.")
        pairs.append((key, value))
    return pairs


def choose_method(use_head: bool, use_options: bool) -> str:
    if use_head and use_options:
        raise ValueError("Use only one of --head or --options.")
    if use_head:
        return "HEAD"
    if use_options:
        return "OPTIONS"
    return "GET"


def build_url(
    base_url: str,
    section: str,
    path: str,
    session: str | None,
    chamber: str | None,
    minifyresult: str | None,
    extra_params: Iterable[Tuple[str, str]],
) -> str:
    section = section.strip("/")
    extra_path = path.strip("/")
    url = base_url.rstrip("/") + "/" + section
    if extra_path:
        url += "/" + extra_path

    params: List[Tuple[str, str]] = []
    if session:
        params.append(("session", session))
    if chamber:
        params.append(("chamber", chamber))
    if minifyresult:
        params.append(("minifyresult", minifyresult))
    params.extend(extra_params)

    if params:
        url += "?" + urllib.parse.urlencode(params)

    return url


def build_request(
    url: str, method: str, version: str, queries: Iterable[str], result_range: str | None
) -> urllib.request.Request:
    headers = {
        "Accept-Encoding": "gzip",
        "X-Alaska-Legislature-Basis-Version": version,
        "User-Agent": "basis-cli/0.1 (+local)",
    }

    query_values = [q.strip() for q in queries if q and q.strip()]
    if query_values:
        headers["X-Alaska-Legislature-Basis-Query"] = ",".join(query_values)

    if result_range:
        headers["X-Alaska-Query-ResultRange"] = result_range

    return urllib.request.Request(url=url, headers=headers, method=method)


def decode_body(body: bytes, content_encoding: str | None) -> bytes:
    if not body:
        return body
    if content_encoding and "gzip" in content_encoding.lower():
        return gzip.decompress(body)
    return body


def pretty_print_xml(xml_bytes: bytes) -> str:
    text = xml_bytes.decode("utf-8", errors="replace")
    parsed = minidom.parseString(text.encode("utf-8"))
    return parsed.toprettyxml(indent="  ")


def save_output(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def print_headers(headers) -> None:
    for key, value in headers.items():
        print(f"{key}: {value}")


def strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def parse_xml_root(xml_source: bytes | str) -> ET.Element | None:
    if isinstance(xml_source, bytes):
        text = xml_source.decode("utf-8", errors="replace")
    else:
        text = xml_source
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def first_child(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if strip_ns(child.tag) == name:
            return child
    return None


def child_text(elem: ET.Element, name: str) -> str:
    child = first_child(elem, name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def xml_error_info(xml_source: bytes | str) -> tuple[str, str] | None:
    root = parse_xml_root(xml_source)
    if root is None:
        return None
    for elem in root.iter():
        if strip_ns(elem.tag) != "Error":
            continue
        return child_text(elem, "Code"), child_text(elem, "Description")
    return None


def extract_meeting_media(xml_source: bytes | str) -> list[dict[str, str]]:
    root = parse_xml_root(xml_source)
    if root is None:
        return []
    items: list[dict[str, str]] = []
    for meeting in root.iter():
        if strip_ns(meeting.tag) != "Meeting":
            continue
        media = first_child(meeting, "Media")
        if media is None:
            continue
        common = {
            "chamber": child_text(meeting, "chamber"),
            "schedule": child_text(meeting, "Schedule"),
            "title": child_text(meeting, "Title"),
            "sponsor": child_text(meeting, "Sponsor"),
            "sponsor_type": first_child(meeting, "Sponsor").attrib.get("type", "") if first_child(meeting, "Sponsor") is not None else "",
        }
        contents = [child for child in media if strip_ns(child.tag) == "Content"]
        if not contents:
            items.append({**common, "media_type": "", "url": "", "duration": "", "start_time": ""})
        for content in contents:
            items.append(
                {
                    **common,
                    "media_type": content.attrib.get("MediaType", ""),
                    "duration": content.attrib.get("Duration", ""),
                    "start_time": content.attrib.get("StartTime", ""),
                    "url": child_text(content, "Url"),
                }
            )
    return items


def extract_meeting_documents(xml_source: bytes | str) -> list[dict[str, str]]:
    root = parse_xml_root(xml_source)
    if root is None:
        return []
    items: list[dict[str, str]] = []
    for meeting in root.iter():
        if strip_ns(meeting.tag) != "Meeting":
            continue
        documents = first_child(meeting, "Documents")
        if documents is None:
            continue
        common = {
            "chamber": child_text(meeting, "chamber"),
            "schedule": child_text(meeting, "Schedule"),
            "title": child_text(meeting, "Title"),
            "sponsor": child_text(meeting, "Sponsor"),
            "sponsor_type": first_child(meeting, "Sponsor").attrib.get("type", "") if first_child(meeting, "Sponsor") is not None else "",
        }
        for content in documents:
            if strip_ns(content.tag) != "Content":
                continue
            items.append(
                {
                    **common,
                    "doc_id": content.attrib.get("DocID", ""),
                    "url": child_text(content, "Url"),
                    "name": child_text(content, "name"),
                }
            )
    return items


def extract_bill_actions(xml_source: bytes | str) -> list[dict[str, str]]:
    root = parse_xml_root(xml_source)
    if root is None:
        return []
    items: list[dict[str, str]] = []
    for bill in root.iter():
        if strip_ns(bill.tag) != "Bill":
            continue
        actions = first_child(bill, "Actions")
        if actions is None:
            continue
        billnumber = bill.attrib.get("billnumber", "").strip()
        chamber = bill.attrib.get("chamber", "").strip()
        for action in actions:
            if strip_ns(action.tag) != "Action":
                continue
            items.append(
                {
                    "billnumber": billnumber,
                    "chamber": chamber,
                    "code": action.attrib.get("code", ""),
                    "action_chamber": action.attrib.get("chamber", ""),
                    "journal_date": action.attrib.get("journaldate", ""),
                    "journal_page": action.attrib.get("journalpage", ""),
                    "text": child_text(action, "ActionText"),
                }
            )
    return items


def extract_bill_sponsor_statements(xml_source: bytes | str) -> list[dict[str, str]]:
    root = parse_xml_root(xml_source)
    if root is None:
        return []
    items: list[dict[str, str]] = []
    for bill in root.iter():
        if strip_ns(bill.tag) != "Bill":
            continue
        sponsors = first_child(bill, "Sponsors")
        if sponsors is None:
            continue
        statement = child_text(sponsors, "SponsorStatement")
        if not statement:
            continue
        items.append(
            {
                "billnumber": bill.attrib.get("billnumber", "").strip(),
                "chamber": bill.attrib.get("chamber", "").strip(),
                "url": statement,
            }
        )
    return items


def fetch_basis(
    *,
    base_url: str,
    section: str,
    path: str = "",
    session: str | None = None,
    chamber: str | None = None,
    minifyresult: str | None = None,
    extra_params: Iterable[Tuple[str, str]] = (),
    version: str = DEFAULT_VERSION,
    queries: Iterable[str] = (),
    result_range: str | None = None,
    method: str = "GET",
    timeout: float = 30.0,
) -> dict[str, object]:
    url = build_url(
        base_url=base_url,
        section=section,
        path=path,
        session=session,
        chamber=chamber,
        minifyresult=minifyresult,
        extra_params=extra_params,
    )
    request = build_request(
        url=url,
        method=method,
        version=version,
        queries=queries,
        result_range=result_range,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw_body = response.read()
        body = decode_body(raw_body, response.headers.get("Content-Encoding"))
        return {
            "method": method,
            "url": url,
            "request_headers": request.header_items(),
            "status": getattr(response, "status", None),
            "reason": getattr(response, "reason", ""),
            "response_headers": response.headers,
            "body": body,
        }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        extra_params = parse_extra_params(args.param)
        method = choose_method(args.head, args.options)
        url = build_url(
            base_url=args.base_url,
            section=args.section,
            path=args.path,
            session=args.session,
            chamber=args.chamber,
            minifyresult=args.minifyresult,
            extra_params=extra_params,
        )
        request = build_request(
            url=url,
            method=method,
            version=args.version,
            queries=args.query,
            result_range=args.result_range,
        )
    except ValueError as exc:
        print(f"Argument error: {exc}", file=sys.stderr)
        return 2

    print(f"Method: {method}")
    print(f"URL: {url}")
    print("Request headers:")
    for key, value in request.header_items():
        print(f"  {key}: {value}")
    print()

    try:
        result = fetch_basis(
            base_url=args.base_url,
            section=args.section,
            path=args.path,
            session=args.session,
            chamber=args.chamber,
            minifyresult=args.minifyresult,
            extra_params=extra_params,
            version=args.version,
            queries=args.query,
            result_range=args.result_range,
            method=method,
            timeout=args.timeout,
        )
        print(f"HTTP: {result['status']} {result['reason']}".rstrip())
        print("Response headers:")
        print_headers(result["response_headers"])
        print()

        if method in ("HEAD", "OPTIONS"):
            return 0

        body = result["body"]

        if args.out:
            save_output(args.out, body)
            print(f"Saved raw response to: {args.out}")
            print()

        if not body:
            print("(empty response body)")
            return 0

        if args.raw:
            sys.stdout.write(body.decode("utf-8", errors="replace"))
            return 0

        try:
            pretty = pretty_print_xml(body)
            sys.stdout.write(pretty)
        except ExpatError:
            sys.stdout.write(body.decode("utf-8", errors="replace"))

        return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
        try:
            body = exc.read()
            if body:
                print(body.decode("utf-8", errors="replace"), file=sys.stderr)
        except Exception:
            pass
        return 1
    except urllib.error.URLError as exc:
        print(f"Connection error: {exc.reason}", file=sys.stderr)
        return 1
    except gzip.BadGzipFile as exc:
        print(f"Gzip decode error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
