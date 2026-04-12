#!/usr/bin/env python3
"""Tiny terminal chat wrapper for the Alaska Legislature BASIS API."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

import query_basis


OPENAI_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-4o-mini"
VALID_SECTIONS = {"bills", "members", "committees", "sessions", "meetings"}
VALID_METHODS = {"GET", "HEAD", "OPTIONS"}
VALID_MINIFY = {"", "true", "false"}
VALID_QUERY_ROOTS = {
    "Actions",
    "Bills",
    "Committees",
    "Documents",
    "FiscalNotes",
    "Media",
    "Meetings",
    "Members",
    "Minutes",
    "Sessions",
    "Sponsors",
    "Subjects",
    "Versions",
}

PLANNER_INSTRUCTIONS = """You translate plain-English requests into Alaska Legislature BASIS API requests.

Return JSON only, matching the provided schema exactly.

Rules:
- Prefer session 34 unless the user explicitly requests another session.
- Use only these sections: bills, members, committees, sessions, meetings.
- Use only these query roots when you build entries in the queries array:
  Actions, Bills, Committees, Documents, FiscalNotes, Media, Meetings, Members, Minutes, Sessions, Sponsors, Subjects, Versions
- Use GET unless the user clearly asks for HEAD or OPTIONS.
- Use result_range for list requests unless the user clearly asks for all results.
- Keep path empty unless it is truly needed.
- Put additional URL query params in params as objects with key/value.
- If the user asks for bill history, referrals, or journal-style action history, include Actions.
- If the user asks for sponsors, subjects, versions, or fiscal notes, include the matching expansion.
- If the user asks for meeting minutes, meeting media links, or meeting documents, include Minutes, Media, or Documents on meetings.
- Meetings Documents is confirmed on small-range live probes, but broader filtered queries can still fault, so avoid overclaiming reliability.
- Do not invent unsupported API sections or unsupported expansions.
"""

ANSWER_INSTRUCTIONS = """You are a concise assistant summarizing Alaska Legislature BASIS API results.

Use only the provided BASIS request and response data.
- Be direct and concise.
- If the API does not expose a requested field, say so plainly.
- If the response is empty, say that clearly.
- When the request returns a list, summarize the most relevant items and counts.
- Prefer the derived_data summaries when they are present, because they were extracted directly from the XML.
- Do not claim to have used any source other than the provided BASIS API response.
"""

PLANNER_SCHEMA = {
    "type": "object",
    "properties": {
        "section": {"type": "string", "enum": sorted(VALID_SECTIONS)},
        "path": {"type": "string"},
        "session": {"type": "string"},
        "chamber": {"type": "string"},
        "minifyresult": {"type": "string", "enum": sorted(VALID_MINIFY)},
        "params": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
        "queries": {"type": "array", "items": {"type": "string"}},
        "result_range": {"type": "string"},
        "method": {"type": "string", "enum": sorted(VALID_METHODS)},
        "explain": {"type": "string"},
    },
    "required": [
        "section",
        "path",
        "session",
        "chamber",
        "minifyresult",
        "params",
        "queries",
        "result_range",
        "method",
        "explain",
    ],
    "additionalProperties": False,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plain-English terminal chat wrapper for the BASIS API."
    )
    parser.add_argument(
        "--planner-model",
        default=os.getenv("OPENAI_PLANNER_MODEL", DEFAULT_MODEL),
        help="OpenAI model for request planning (default: %(default)s).",
    )
    parser.add_argument(
        "--answer-model",
        default=os.getenv("OPENAI_ANSWER_MODEL", DEFAULT_MODEL),
        help="OpenAI model for answer generation (default: %(default)s).",
    )
    parser.add_argument(
        "--request",
        help="Optional one-shot plain-English request. If omitted, starts an interactive chat loop.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Timeout in seconds for OpenAI and BASIS requests (default: %(default)s).",
    )
    return parser


def read_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    return api_key


def make_openai_request(payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def extract_output_text(response_json: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in response_json.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                texts.append(text)
    if texts:
        return "\n".join(texts).strip()
    raise ValueError("OpenAI response did not include any text output.")


def plan_basis_request(user_text: str, model: str, api_key: str, timeout: float) -> dict[str, Any]:
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": PLANNER_INSTRUCTIONS},
            {"role": "user", "content": user_text},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "basis_request",
                "strict": True,
                "schema": PLANNER_SCHEMA,
            }
        },
    }
    response_json = make_openai_request(payload, api_key, timeout)
    return json.loads(extract_output_text(response_json))


def normalize_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def unique_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for query in queries:
        if query not in seen:
            seen.add(query)
            ordered.append(query)
    return ordered


def has_query_root(queries: list[str], root: str) -> bool:
    return any(query.split(";", 1)[0].strip() == root for query in queries)


def has_meetings_filter(queries: list[str]) -> bool:
    return any(query.split(";", 1)[0].strip() == "Meetings" for query in queries)


def default_meetings_window(chamber: str | None = None) -> str:
    today = dt.date.today()
    start = today - dt.timedelta(days=3)
    end = today + dt.timedelta(days=7)
    parts = [f"startdate={start:%m/%d/%y}", f"enddate={end:%m/%d/%y}"]
    if chamber:
        parts.append(f"chamber={chamber}")
    return "Meetings;" + ";".join(parts)


def apply_plan_hints(plan: dict[str, Any], user_text: str) -> dict[str, Any]:
    updated = dict(plan)
    queries = list(updated["queries"])
    text = user_text.lower()

    wants_history = any(term in text for term in ("referral", "referred", "history", "journal", "action"))
    wants_media = any(term in text for term in ("video", "audio", "mp3", "m4v", "media", "hearing link"))
    wants_documents = any(term in text for term in ("document", "documents", "packet", "hearing docs", "handout"))
    wants_minutes = "minutes" in text
    wants_sponsor_statement = "sponsor statement" in text

    if wants_history and updated["section"] == "bills" and not has_query_root(queries, "Actions"):
        queries.append("Actions")

    if wants_sponsor_statement and updated["section"] == "bills" and not has_query_root(queries, "Sponsors"):
        queries.append("Sponsors")

    if wants_media or wants_documents or wants_minutes:
        updated["section"] = "meetings"
        if wants_media and not has_query_root(queries, "Media"):
            queries.append("Media")
        if wants_documents and not has_query_root(queries, "Documents"):
            queries.append("Documents")
        if wants_minutes and not has_query_root(queries, "Minutes"):
            queries.append("Minutes")
        if not has_meetings_filter(queries):
            queries.insert(0, default_meetings_window(updated["chamber"] or None))
        if not updated["result_range"]:
            updated["result_range"] = "..3"

    updated["queries"] = unique_queries(queries)
    return updated


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("Planner output was not a JSON object.")

    section = normalize_string(plan.get("section"))
    if section not in VALID_SECTIONS:
        raise ValueError(f"Unsupported section: {section or '(empty)'}")

    method = normalize_string(plan.get("method")).upper() or "GET"
    if method not in VALID_METHODS:
        raise ValueError(f"Unsupported method: {method}")

    minifyresult = normalize_string(plan.get("minifyresult"))
    if minifyresult not in VALID_MINIFY:
        raise ValueError(f"Invalid minifyresult: {minifyresult!r}")

    params = plan.get("params", [])
    if not isinstance(params, list):
        raise ValueError("params must be an array.")
    extra_params = []
    for item in params:
        if not isinstance(item, dict):
            raise ValueError("Each params item must be an object.")
        key = normalize_string(item.get("key"))
        value = normalize_string(item.get("value"))
        if not key:
            raise ValueError("A params item had an empty key.")
        extra_params.append((key, value))

    queries = plan.get("queries", [])
    if not isinstance(queries, list):
        raise ValueError("queries must be an array.")
    normalized_queries = []
    for item in queries:
        query = normalize_string(item)
        if not query:
            continue
        root = query.split(";", 1)[0].strip()
        if root not in VALID_QUERY_ROOTS:
            raise ValueError(f"Unsupported query root: {root}")
        normalized_queries.append(query)

    normalized = {
        "section": section,
        "path": normalize_string(plan.get("path")),
        "session": normalize_string(plan.get("session")) or "34",
        "chamber": normalize_string(plan.get("chamber")),
        "minifyresult": minifyresult or None,
        "extra_params": extra_params,
        "queries": normalized_queries,
        "result_range": normalize_string(plan.get("result_range")) or None,
        "method": method,
        "explain": normalize_string(plan.get("explain")),
    }
    return normalized


def derive_data(plan: dict[str, Any], body_text: str) -> dict[str, Any]:
    derived: dict[str, Any] = {}
    if plan["section"] == "meetings":
        media = query_basis.extract_meeting_media(body_text)
        documents = query_basis.extract_meeting_documents(body_text)
        if media:
            derived["meeting_media"] = media[:20]
        if documents:
            derived["meeting_documents"] = documents[:20]
    if plan["section"] == "bills":
        actions = query_basis.extract_bill_actions(body_text)
        sponsor_statements = query_basis.extract_bill_sponsor_statements(body_text)
        if actions:
            derived["bill_actions"] = actions[:40]
        if sponsor_statements:
            derived["bill_sponsor_statements"] = sponsor_statements[:20]
    return derived


def fallback_summary(plan: dict[str, Any], basis_result: dict[str, Any]) -> str:
    derived = basis_result.get("derived_data", {})
    if isinstance(derived, dict):
        meeting_documents = derived.get("meeting_documents") or []
        if meeting_documents:
            lines = ["Found meeting documents:"]
            for item in meeting_documents[:8]:
                lines.append(f"- {item['title']} | {item['schedule']} | {item['name']} | {item['url']}")
            return "\n".join(lines)

        meeting_media = derived.get("meeting_media") or []
        if meeting_media:
            lines = ["Found meeting media links:"]
            for item in meeting_media[:8]:
                if item.get("url"):
                    media_label = item.get("media_type") or "media"
                    lines.append(f"- {item['title']} | {item['schedule']} | {media_label} | {item['url']}")
            if len(lines) > 1:
                return "\n".join(lines)

        bill_actions = derived.get("bill_actions") or []
        if bill_actions:
            lines = ["Found bill actions:"]
            for item in bill_actions[:8]:
                lines.append(f"- {item['billnumber']} | {item['journal_date']} p.{item['journal_page']} | {item['text']}")
            return "\n".join(lines)

        sponsor_statements = derived.get("bill_sponsor_statements") or []
        if sponsor_statements:
            lines = ["Found sponsor statements:"]
            for item in sponsor_statements[:8]:
                lines.append(f"- {item['billnumber']} | {item['url']}")
            return "\n".join(lines)

    query_count = basis_result["headers"].get("X-Alaska-Query-Count", "")
    status = f"{basis_result['status']} {basis_result['reason']}".strip()
    return f"BASIS response received: {status}. Query count: {query_count or 'unknown'}."


def build_retry_plans(plan: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    retries: list[tuple[dict[str, Any], str]] = []

    if plan["section"] == "meetings":
        if plan["result_range"] not in {"..1", "..3"}:
            smaller = dict(plan)
            smaller["result_range"] = "..3"
            retries.append((smaller, "Retrying with a smaller meetings result range (`..3`)."))

        if not has_meetings_filter(list(plan["queries"])):
            narrower = dict(plan)
            narrower["queries"] = unique_queries(
                [default_meetings_window(plan["chamber"] or None), *list(plan["queries"])]
            )
            narrower["result_range"] = "..3"
            retries.append((narrower, "Retrying with a narrower meetings date window and small range."))

    if plan["section"] in {"members", "committees"} and plan["result_range"] not in {"..1", None}:
        smaller = dict(plan)
        smaller["result_range"] = "..1"
        retries.append((smaller, f"Retrying {plan['section']} with a smaller result range (`..1`)."))

    deduped: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for retry_plan, note in retries:
        key = json.dumps(retry_plan, sort_keys=True)
        if key not in seen:
            seen.add(key)
            deduped.append((retry_plan, note))
    return deduped


def run_basis(plan: dict[str, Any], timeout: float) -> dict[str, Any]:
    result = query_basis.fetch_basis(
        base_url=query_basis.DEFAULT_BASE_URL,
        section=plan["section"],
        path=plan["path"],
        session=plan["session"],
        chamber=plan["chamber"] or None,
        minifyresult=plan["minifyresult"],
        extra_params=plan["extra_params"],
        version=query_basis.DEFAULT_VERSION,
        queries=plan["queries"],
        result_range=plan["result_range"],
        method=plan["method"],
        timeout=timeout,
    )
    body_text = result["body"].decode("utf-8", errors="replace")
    return {
        "method": result["method"],
        "url": result["url"],
        "status": result["status"],
        "reason": result["reason"],
        "headers": dict(result["response_headers"].items()),
        "body_text": body_text,
        "xml_error": query_basis.xml_error_info(body_text),
        "derived_data": derive_data(plan, body_text),
    }


def run_basis_with_retries(plan: dict[str, Any], timeout: float) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    notes: list[str] = []
    attempts = [plan, *[retry_plan for retry_plan, _ in build_retry_plans(plan)]]
    reasons = ["Initial BASIS request.", *[note for _, note in build_retry_plans(plan)]]

    for attempt_plan, reason in zip(attempts, reasons):
        if reason != "Initial BASIS request.":
            print(reason, file=sys.stderr)
        result = run_basis(attempt_plan, timeout)
        xml_error = result.get("xml_error")
        if not xml_error or xml_error[0] != "FaultException":
            if reason != "Initial BASIS request.":
                notes.append(reason)
            return result, attempt_plan, notes
        notes.append(f"{reason} Received FaultException.")

    final_result = run_basis(attempts[-1], timeout)
    return final_result, attempts[-1], notes


def build_answer_prompt(user_text: str, plan: dict[str, Any], basis_result: dict[str, Any]) -> str:
    response_text = basis_result["body_text"]
    if len(response_text) > 60000:
        response_text = response_text[:60000] + "\n\n[TRUNCATED]"

    payload = {
        "user_request": user_text,
        "basis_request": {
            "section": plan["section"],
            "path": plan["path"],
            "session": plan["session"],
            "chamber": plan["chamber"],
            "minifyresult": plan["minifyresult"],
            "params": plan["extra_params"],
            "queries": plan["queries"],
            "result_range": plan["result_range"],
            "method": plan["method"],
            "explain": plan["explain"],
        },
        "basis_response": {
            "status": basis_result["status"],
            "reason": basis_result["reason"],
            "url": basis_result["url"],
            "headers": basis_result["headers"],
            "body_text": response_text,
            "xml_error": basis_result.get("xml_error"),
            "derived_data": basis_result.get("derived_data", {}),
        },
    }
    return json.dumps(payload, ensure_ascii=True)


def summarize_basis(user_text: str, plan: dict[str, Any], basis_result: dict[str, Any], model: str, api_key: str, timeout: float) -> str:
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": ANSWER_INSTRUCTIONS},
            {"role": "user", "content": build_answer_prompt(user_text, plan, basis_result)},
        ],
    }
    response_json = make_openai_request(payload, api_key, timeout)
    return extract_output_text(response_json)


def handle_request(user_text: str, args: argparse.Namespace, api_key: str) -> int:
    user_text = user_text.strip()
    if not user_text:
        return 0
    if user_text.lower() in {"quit", "exit"}:
        return 1

    try:
        plan = apply_plan_hints(
            validate_plan(
            plan_basis_request(user_text, args.planner_model, api_key, args.timeout)
            ),
            user_text,
        )
    except urllib.error.HTTPError as exc:
        print(f"OpenAI planner HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
        return 0
    except urllib.error.URLError as exc:
        print(f"OpenAI planner connection error: {exc.reason}", file=sys.stderr)
        return 0
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Planner output error: {exc}", file=sys.stderr)
        return 0

    print(f"Planned request: {plan['method']} {plan['section']}", file=sys.stderr)

    try:
        basis_result, final_plan, retry_notes = run_basis_with_retries(plan, args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"BASIS HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
        try:
            body = exc.read().decode("utf-8", errors="replace")
            if body.strip():
                print(body, file=sys.stderr)
        except Exception:
            pass
        return 0
    except urllib.error.URLError as exc:
        print(f"BASIS connection error: {exc.reason}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"BASIS request error: {exc}", file=sys.stderr)
        return 0

    if retry_notes:
        for note in retry_notes:
            print(note, file=sys.stderr)

    xml_error = basis_result.get("xml_error")
    if xml_error:
        print(f"BASIS XML error: {xml_error[0]}: {xml_error[1]}", file=sys.stderr)
        return 0

    try:
        answer = summarize_basis(
            user_text, final_plan, basis_result, args.answer_model, api_key, args.timeout
        )
    except urllib.error.HTTPError as exc:
        print(f"OpenAI answer HTTP error: {exc.code} {exc.reason}", file=sys.stderr)
        return 0
    except urllib.error.URLError as exc:
        print(f"OpenAI answer connection error: {exc.reason}", file=sys.stderr)
        return 0
    except ValueError as exc:
        print(f"Answer parsing error: {exc}", file=sys.stderr)
        print(fallback_summary(final_plan, basis_result))
        return 0

    print(answer)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        api_key = read_api_key()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.request:
        return handle_request(args.request, args, api_key)

    print("Enter a BASIS question. Type 'quit' or 'exit' to stop.")
    while True:
        try:
            user_text = input("> ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130

        should_exit = handle_request(user_text, args, api_key)
        if should_exit:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
