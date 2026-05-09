"""BASIS Browser App — single-file Flask server."""

import os
import time
import threading
import cache
from flask import Flask, render_template, jsonify, request
from adapter import (
    house_bills_in_senate, senate_bills_in_house, dashboard_stats,
    action_code_counts, bill_progress, activity_feed,
    governor_bills, _fetch_bill_detail, committee_detail, top_subjects,
)

app = Flask(__name__, template_folder="templates", static_folder="static")

REFRESH_INTERVAL = 3600  # 1 hour


def _refresh_all():
    """Re-fetch all major data sources, populating caches."""
    refreshers = [
        ("crossover", lambda: (house_bills_in_senate(), senate_bills_in_house())),
        ("dashboard", dashboard_stats),
        ("action_codes", action_code_counts),
        ("bill_progress", bill_progress),
        ("activity_feed", activity_feed),
        ("governor", governor_bills),
    ]
    for name, fn in refreshers:
        try:
            fn()
        except Exception as exc:
            print(f"[refresh] {name} failed: {exc}", flush=True)


def _invalidate_top_level_caches():
    """Drop the top-level dashboard/page caches so refresh re-computes them.
    Underlying caches (bills, hearing windows) keep their own freshness rules."""
    keys_to_clear = [
        "hb_in_senate", "sb_in_house", "dashboard_stats", "action_code_counts",
        "bill_progress", "all_actions", "governor_bills",
    ]
    # Also clear any activity_feed_X entries
    for k in list(cache._cache.keys()):
        if k.startswith("activity_feed_") or k in keys_to_clear:
            cache._cache.pop(k, None)


def prefetch():
    """Warm the cache once on startup, then refresh hourly."""
    print("[refresh] startup prefetch", flush=True)
    _refresh_all()
    print("[refresh] startup prefetch complete", flush=True)

    while True:
        time.sleep(REFRESH_INTERVAL)
        print("[refresh] hourly refresh starting", flush=True)
        _invalidate_top_level_caches()
        _refresh_all()
        print("[refresh] hourly refresh complete", flush=True)


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
        return jsonify({"stats": stats, "error": None})
    except Exception as exc:
        return jsonify({"stats": None, "error": str(exc)})


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


_start_prefetch_once()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=os.environ.get("FLASK_DEBUG") == "1")
