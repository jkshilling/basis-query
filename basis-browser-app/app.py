"""BASIS Browser App — single-file Flask server."""

import os
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import cache
from flask import Flask, render_template, jsonify, request

# Structured-ish logging: timestamp logger=name level message key=value pairs.
logging.basicConfig(
    level=os.environ.get("BASIS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("basis_browser.app")
from adapter import (
    house_bills_in_senate, senate_bills_in_house, dashboard_stats,
    action_code_counts, bill_progress, activity_feed,
    governor_bills, _fetch_bill_detail, committee_detail, top_subjects,
    search_bills, cache_freshness,
)
from metrics import (
    legs_score, pipeline, awaiting_transmittal, session_countdown,
    bill_decision_detail,
)

app = Flask(__name__, template_folder="templates", static_folder="static")

REFRESH_INTERVAL = 3600  # 1 hour


def _refresh_all():
    """Re-fetch all major data sources, populating caches.

    Many of these share underlying caches (e.g. _scan_all_actions). We run
    crossover and dashboard first sequentially so the heavy underlying
    caches get populated, then fan out the rest in parallel.
    """
    t0 = time.monotonic()

    # Stage 1: warm the shared underlying caches (sequential).
    for name, fn in [
        ("crossover_h", house_bills_in_senate),
        ("crossover_s", senate_bills_in_house),
        ("dashboard", dashboard_stats),
    ]:
        s = time.monotonic()
        try:
            fn()
            log.info("refresh.step name=%s elapsed=%.1fs status=ok",
                     name, time.monotonic() - s)
        except Exception as exc:
            log.warning("refresh.step name=%s elapsed=%.1fs status=fail err=%r",
                        name, time.monotonic() - s, exc)

    # Stage 2: independent fetches, parallel.
    from fetch import fetch_members
    parallel = [
        ("action_codes", action_code_counts),
        ("bill_progress", bill_progress),
        ("activity_feed", activity_feed),
        ("governor", governor_bills),
        ("pipeline", pipeline),
        ("awaiting_transmittal", awaiting_transmittal),
        ("members", fetch_members),
    ]
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fn): name for name, fn in parallel}
        for fut in futures:
            name = futures[fut]
            s = time.monotonic()
            try:
                fut.result()
                log.info("refresh.step name=%s elapsed=%.1fs status=ok",
                         name, time.monotonic() - s)
            except Exception as exc:
                log.warning("refresh.step name=%s status=fail err=%r", name, exc)

    # Stage 3: pre-warm bill_decision_detail for every awaiting bill,
    # so the first user click on any expander is instant. Each call is
    # a few ms (all underlying caches are now warm from stages 1+2);
    # total cost is ~1-2 s for ~120 bills. Sequential because the
    # caches it builds get reused — parallel would race on shared
    # cache writes.
    s = time.monotonic()
    detail_count = 0
    all_bill_entries = []
    try:
        at_data = awaiting_transmittal()
        all_bill_entries = (at_data.get("bills", []) +
                            at_data.get("at_gov_bills", []) +
                            at_data.get("resolutions_substantive", []) +
                            at_data.get("resolutions_procedural", []))
        for entry in all_bill_entries:
            bn = entry.get("billnumber") or ""
            if not bn:
                continue
            try:
                bill_decision_detail(bn)
                detail_count += 1
            except Exception as exc:
                log.warning("refresh.detail name=%s err=%r", bn, exc)
        log.info("refresh.step name=bill_details count=%d elapsed=%.1fs status=ok",
                 detail_count, time.monotonic() - s)
    except Exception as exc:
        log.warning("refresh.step name=bill_details elapsed=%.1fs status=fail err=%r",
                    time.monotonic() - s, exc)

    # Stage 4: LLM-generated neutral summaries via the Anthropic API.
    # Each call is ~1-2 s and ~$0.008. Already-cached summaries (same
    # input-hash) skip the API entirely. Sequential to avoid rate-limit
    # contention; total cold cost is ~3-5 minutes for a full rebuild,
    # zero after that until blue-sheet content changes.
    s = time.monotonic()
    summ_made = summ_cached = 0
    try:
        import bill_summarizer as _summ
        import blue_sheets as _bs
        from fetch import fetch_all_bills
        # Index meta dicts by billnumber for fast lookup.
        meta_by_bn = {}
        for chamber in ("H", "S"):
            for b in fetch_all_bills(chamber, "34",
                                      queries=["Sponsors", "Subjects",
                                               "Versions", "FiscalNotes"]):
                meta_by_bn[b.get("billnumber", "")] = b
        for entry in all_bill_entries:
            bn = entry.get("billnumber") or ""
            if not bn:
                continue
            sheets = _bs.sheets_for(bn)
            # Skip bills with no blue-sheet content (LLM has nothing
            # substantive to synthesize). The hand-authored fallback
            # dict in bill_summaries.py covers these when needed.
            if not any(s.get("description") for s in sheets):
                continue
            meta = meta_by_bn.get(bn)
            # Was it already cached?
            if _summ.get_cached(bn, sheets, meta):
                summ_cached += 1
                continue
            try:
                if _summ.summarize_bill(bn, sheets, meta):
                    summ_made += 1
            except Exception as exc:
                log.warning("refresh.summary name=%s err=%r", bn, exc)
        log.info("refresh.step name=llm_summaries new=%d cached=%d "
                 "elapsed=%.1fs status=ok",
                 summ_made, summ_cached, time.monotonic() - s)
    except Exception as exc:
        log.warning("refresh.step name=llm_summaries elapsed=%.1fs "
                    "status=fail err=%r", time.monotonic() - s, exc)

    # Stage 5: refresh bill_decision_detail again, now that LLM
    # summaries are cached — so they appear in the detail payloads
    # without waiting for the next hourly cycle.
    if summ_made:
        s = time.monotonic()
        for entry in all_bill_entries:
            bn = entry.get("billnumber") or ""
            if not bn:
                continue
            try:
                # Drop the cached entry to force re-build with the
                # newly-available LLM summary.
                from cache import _cache as _c
                _c.pop(f"bill_decision_detail_v10_34_{bn}", None)
                bill_decision_detail(bn)
            except Exception:
                pass
        # Also drop the top-level awaiting_transmittal cache so the
        # NEXT page render picks up the new llm_summary fields.
        try:
            from cache import _cache as _c
            _c.pop("awaiting_transmittal_v39", None)
            awaiting_transmittal()
        except Exception:
            pass
        log.info("refresh.step name=detail_rewarm elapsed=%.1fs status=ok",
                 time.monotonic() - s)

    log.info("refresh.complete total_elapsed=%.1fs", time.monotonic() - t0)


def _build_votes_index_async():
    """Build the votes index on its own daemon thread. The first build
    takes 3+ minutes and would otherwise tie up a gunicorn worker; this
    lets it run independently. Repeated hourly so the index stays
    reasonably fresh."""
    from fetch import fetch_all_votes_index
    while True:
        s = time.monotonic()
        try:
            fetch_all_votes_index()
            log.info("votes_index.build elapsed=%.1fs status=ok",
                     time.monotonic() - s)
        except Exception as exc:
            log.warning("votes_index.build status=fail err=%r", exc)
        # Index has a 1-hour TTL inside fetch; rebuild before that.
        time.sleep(REFRESH_INTERVAL)


def _invalidate_top_level_caches():
    """Drop the top-level dashboard/page caches so refresh re-computes them.
    Underlying caches (bills, hearing windows) keep their own freshness rules."""
    keys_to_clear = [
        "hb_in_senate", "sb_in_house", "dashboard_stats", "action_code_counts",
        "bill_progress", "all_actions", "all_actions_v5", "governor_bills",
        "awaiting_transmittal_v39", "pipeline_v3_20",
    ]
    # Also clear any activity_feed_X entries and today's floor calendar
    # (so refreshes pick up newly-calendared bills).
    for k in list(cache._cache.keys()):
        if (k.startswith("activity_feed_")
                or k.startswith("floor_cal_")
                or k in keys_to_clear):
            cache._cache.pop(k, None)


def prefetch():
    """Warm the cache once on startup, then refresh hourly."""
    log.info("refresh.startup_begin")
    _refresh_all()
    log.info("refresh.startup_done")

    while True:
        time.sleep(REFRESH_INTERVAL)
        log.info("refresh.hourly_begin")
        _invalidate_top_level_caches()
        _refresh_all()
        log.info("refresh.hourly_done")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/crossover")
def api_crossover():
    try:
        hb = house_bills_in_senate()
    except Exception:
        hb = []
    try:
        sb = senate_bills_in_house()
    except Exception:
        sb = []
    return jsonify({"hb_in_senate": hb, "sb_in_house": sb})


@app.route("/bill-progress")
def bill_progress_page():
    return render_template("bill_progress.html")


@app.route("/api/bill-progress")
def api_bill_progress():
    try:
        data = bill_progress()
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": None, "error": str(exc)})


@app.route("/activity")
def activity_page():
    return render_template("activity.html")


@app.route("/api/activity")
def api_activity():
    days = int(request.args.get("days", 7))
    try:
        data = activity_feed(days=days)
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": None, "error": str(exc)})


@app.route("/governor")
def governor_page():
    return render_template("governor.html")


@app.route("/api/governor")
def api_governor():
    try:
        data = governor_bills()
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": None, "error": str(exc)})


@app.route("/action-codes")
def action_codes():
    return render_template("action_codes.html")


@app.route("/api/action-codes")
def api_action_codes():
    try:
        counts = action_code_counts()
    except Exception:
        counts = {"2025": {}, "2026": {}}
    return jsonify(counts)


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/dashboard")
def api_dashboard():
    try:
        stats = dashboard_stats()
        # Floor calendars need to be fresh — dashboard_stats may have
        # been cached when the legislature hadn't yet published a
        # chamber's calendar. Always re-fetch them on each dashboard
        # request (fetch_floor_calendar has its own short cache).
        from fetch import fetch_floor_calendar
        stats = dict(stats)
        stats["house_floor"] = fetch_floor_calendar("H")
        stats["senate_floor"] = fetch_floor_calendar("S")
        return jsonify({"stats": stats, "error": None})
    except Exception as exc:
        return jsonify({"stats": None, "error": str(exc)})


@app.route("/search")
def search_page():
    return render_template("search.html", q=request.args.get("q", ""))


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    try:
        results = search_bills(q)
        return jsonify({"results": results, "error": None})
    except Exception as exc:
        return jsonify({"results": [], "error": str(exc)})


@app.route("/api/freshness")
def api_freshness():
    try:
        return jsonify(cache_freshness())
    except Exception:
        return jsonify({})


@app.route("/schedule")
def schedule_page():
    return render_template("schedule.html")


@app.route("/api/schedule")
def api_schedule():
    from fetch import fetch_committee_schedule
    date = request.args.get("date") or None
    try:
        meetings = fetch_committee_schedule(date)
        return jsonify({"date": date, "meetings": meetings, "error": None})
    except Exception as exc:
        return jsonify({"date": date, "meetings": [], "error": str(exc)})


@app.route("/pipeline")
def pipeline_page():
    return render_template("pipeline.html")


@app.route("/api/pipeline")
def api_pipeline():
    try:
        data = pipeline()
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": None, "error": str(exc)})


@app.route("/awaiting-transmittal")
def awaiting_transmittal_page():
    return render_template("awaiting_transmittal.html")


@app.route("/api/awaiting-transmittal")
def api_awaiting_transmittal():
    try:
        data = awaiting_transmittal()
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": None, "error": str(exc)})


@app.route("/api/bill/<path:billnumber>/decision-detail")
def api_bill_decision_detail(billnumber):
    """Lazy-loaded detail bundle for one bill on the awaiting-transmittal
    page: per-legislator roll call, full action timeline, fiscal notes."""
    try:
        data = bill_decision_detail(billnumber)
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": None, "error": str(exc)})


@app.route("/api/bill/<path:billnumber>/flag", methods=["POST"])
def api_bill_flag(billnumber):
    """Set or cycle a bill's workflow flag.

    Body (optional): {"state": "<state>"} — set explicit state.
                      Pass empty string to clear.
    No body: cycle to next state in the rotation
             ("" → needs-research → reviewed → decided → "").

    Returns: {"state": <new_state>, "billnumber": <bn>, "error": null}
    """
    import bill_flags
    try:
        payload = request.get_json(silent=True) or {}
        if "state" in payload:
            new = bill_flags.set_flag(billnumber, payload["state"])
        else:
            new = bill_flags.cycle_flag(billnumber)
        return jsonify({
            "billnumber": billnumber, "state": new, "error": None,
        })
    except ValueError as exc:
        return jsonify({
            "billnumber": billnumber, "state": None, "error": str(exc),
        }), 400
    except Exception as exc:
        return jsonify({
            "billnumber": billnumber, "state": None, "error": str(exc),
        }), 500


def _docx_to_html(path, filename):
    """Render a .docx as a minimal HTML page so browsers can display
    it inline instead of forcing a download. Uses zipfile + the
    document.xml inside (no external dependency)."""
    import zipfile
    import re as _re
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open("word/document.xml") as f:
                raw = f.read().decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return None
    # Split into paragraphs at <w:p> boundaries, then strip the
    # remaining XML tags. Preserves paragraph structure; loses inline
    # formatting (bold/italic) which is fine for a quick reader view.
    parts = _re.split(r"<w:p[\s>][^/]*?>", raw)
    paragraphs = []
    for chunk in parts:
        # Strip everything tag-like.
        text = _re.sub(r"<[^>]+>", "", chunk)
        text = (text.replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&quot;", '"')
                    .replace("&apos;", "'"))
        text = " ".join(text.split())
        if text:
            paragraphs.append(text)

    from html import escape as _h_escape
    body = "\n".join(f"<p>{_h_escape(p)}</p>" for p in paragraphs)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_h_escape(filename)}</title>
<style>
  body {{
    font-family: Georgia, "Times New Roman", serif;
    max-width: 780px;
    margin: 40px auto;
    padding: 0 20px;
    color: #1a2530;
    line-height: 1.55;
  }}
  header {{
    border-bottom: 1px solid #d6dbe1;
    padding-bottom: 10px;
    margin-bottom: 20px;
    color: #5b6770;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 0.85rem;
  }}
  header strong {{ color: #1a2530; font-size: 1rem; display: block; }}
  p {{ margin: 0 0 12px; }}
</style>
</head>
<body>
  <header>
    <strong>{_h_escape(filename)}</strong>
    Rendered from .docx for inline viewing. Original file extracted via document.xml.
  </header>
  {body}
</body>
</html>"""
    return html


def _serve_inline_doc(path, filename):
    """Common serve logic for blue-sheet + legal-analysis files. PDFs
    render natively in browsers. DOCX gets converted to HTML so the
    browser displays it inline instead of forcing a download."""
    from flask import send_file, abort, Response
    if not path:
        abort(404)
    if path.lower().endswith(".pdf"):
        # Inline PDF — modern browsers render with their native viewer.
        return send_file(path, mimetype="application/pdf",
                         as_attachment=False)
    if path.lower().endswith((".docx", ".doc")):
        html = _docx_to_html(path, filename)
        if html is None:
            abort(500, "Could not parse DOCX")
        return Response(html, mimetype="text/html; charset=utf-8")
    # Unknown extension — fall through to octet-stream, browser may download.
    return send_file(path, mimetype="application/octet-stream",
                     as_attachment=False)


@app.route("/legal-analysis-file/<path:filename>")
def serve_legal_analysis_file(filename):
    """Serve a specific legal-analysis file by filename."""
    import legal_analyses as la_mod
    return _serve_inline_doc(la_mod.abs_path(filename), filename)


@app.route("/blue-sheet-file/<path:filename>")
def serve_blue_sheet_file(filename):
    """Serve a specific blue-sheet file by filename. Multiple sheets
    can exist for one bill (separate agencies submit separate
    analyses), so the URL is file-keyed, not bill-keyed. abs_path
    prevents path traversal."""
    import blue_sheets as bs_mod
    return _serve_inline_doc(bs_mod.abs_path(filename), filename)


@app.route("/briefing-packet-file/<path:filename>")
def serve_briefing_packet_file(filename):
    """Serve a specific briefing-packet file by filename. Same
    inline-rendering treatment as blue sheets and legal analyses."""
    import briefing_packets as bp_mod
    return _serve_inline_doc(bp_mod.abs_path(filename), filename)


@app.route("/api/session-countdown")
def api_session_countdown():
    try:
        return jsonify(session_countdown())
    except Exception:
        return jsonify({})


@app.route("/api/floor")
def api_floor():
    """Lightweight floor-calendar endpoint for dashboard auto-refresh."""
    from fetch import fetch_floor_calendar
    try:
        return jsonify({
            "house": fetch_floor_calendar("H"),
            "senate": fetch_floor_calendar("S"),
        })
    except Exception as exc:
        return jsonify({"house": [], "senate": [], "error": str(exc)})


@app.route("/api/top-subjects")
def api_top_subjects():
    try:
        data = top_subjects()
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": [], "error": str(exc)})


@app.route("/bill/<path:billnumber>")
def bill_detail_page(billnumber):
    return render_template("bill_detail.html", billnumber=billnumber)


@app.route("/api/bill/<path:billnumber>")
def api_bill_detail(billnumber):
    try:
        data = _fetch_bill_detail(billnumber)
        if data is None:
            return jsonify({"data": None, "error": "Bill not found"})

        # Decorate with legs_score. Convert detail.actions back into the
        # tuple shape that legs_score expects.
        action_tuples = [
            (a["code"], a["chamber"], a["raw_date"], a["text"])
            for a in data.get("actions", [])
        ]
        data["legs"] = legs_score(action_tuples, data.get("chamber", ""))
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": None, "error": str(exc)})


@app.route("/committee/<chamber>/<code>")
def committee_detail_page(chamber, code):
    return render_template("committee_detail.html", chamber=chamber, code=code)


@app.route("/api/committee/<chamber>/<code>")
def api_committee_detail(chamber, code):
    try:
        data = committee_detail(chamber.upper(), code.upper())
        return jsonify({"data": data, "error": None})
    except Exception as exc:
        return jsonify({"data": None, "error": str(exc)})


# Start the background prefetch thread when the app module is imported
# (works under both `python app.py` and gunicorn).
_prefetch_started = False


def _start_prefetch_once():
    global _prefetch_started
    if _prefetch_started:
        return
    _prefetch_started = True
    threading.Thread(target=prefetch, daemon=True).start()
    # Separate daemon thread for the slow votes-index build so it can
    # run in parallel with the main prefetch and not block requests.
    threading.Thread(target=_build_votes_index_async, daemon=True).start()


_start_prefetch_once()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=os.environ.get("FLASK_DEBUG") == "1")
