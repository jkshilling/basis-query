#!/usr/bin/env python3
"""
Minimal stdlib-only CLI for the Alaska Legislature BASIS API.

Examples:
  python3 query_basis.py --section bills --session 34 --chamber H
  python3 query_basis.py --section bills --session 34 --query "Bills;title=*Oil*" --query "Actions"
  python3 query_basis.py --section members --query "Members;lastname=*Edgmon*"
  python3 query_basis.py --section committees --options
  python3 query_basis.py --section bills --session 34 --chamber H --head
  python3 query_basis.py --section bills --session 34 --range "..10"
  python3 query_basis.py --section bills --session 34 --out bills.xml

Notes:
- The BASIS public API documentation describes:
  * URL format: /basis/<section>[/<subsection>]
  * sections: bills | members | committees | sessions
  * params: session | chamber | minifyresult
  * required header: X-Alaska-Legislature-Basis-Version: 1.0
  * optional repeated headers: X-Alaska-Legislature-Basis-Query
  * optional header: X-Alaska-Query-ResultRange

- If the live host differs from the documented path, change --base-url once.
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
from xml.dom import minidom
from xml.parsers.expat import ExpatError


DEFAULT_BASE_URL = "https://www.akleg.gov/publicservice/basis"
DEFAULT_VERSION = "1.4"
VALID_SECTIONS = ("bills", "members", "committees", "sessions")


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
