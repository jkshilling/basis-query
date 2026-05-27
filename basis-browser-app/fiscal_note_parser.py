"""Parser for Alaska fiscal-note PDFs.

Every Alaska fiscal note follows a fixed template. Page 1 carries the
expenditure/revenue grid; subsequent pages hold prose analysis. The
grid we care about always looks like:

    OPERATING EXPENDITURES FY 2027 FY 2027 FY 2028 ... FY 2032
    Personal Services        17.6   17.6   17.6  ...   17.6
    ...
    Total Operating          70.5    0.0   70.1  ...   62.1

The 1st FY27 column is "FY2027 Requested" — what the bill actually
costs that year. The 2nd FY27 column is "Included in Governor's
Appropriation Request" (almost always 0.0 — i.e. NOT in the budget
yet), which we discard. The remaining 5 columns are FY28-FY32 out-
year cost estimates.

Capital and supplemental costs live in fixed-text lines later on:
    Estimated SUPPLEMENTAL (FY2026) cost: 0.0
    Estimated CAPITAL (FY2027) cost: 0.0

All amounts are in thousands of dollars (the header says
"(Thousands of Dollars)"). We return totals in dollars (not
thousands) so the caller can render '$1.2M' / '$326K' etc.

Returns None if the PDF can't be parsed (image-only PDF, no
"Total Operating" line found, etc.) — the caller marks the FN as
"unparseable" rather than baking a wrong number into a total.
"""

from __future__ import annotations

import io
import re


# Match "Total Operating" followed by up to 7 numeric cells. Numbers
# can be:
#   * positive or negative (signed by '-' or '(' parens used in some FNs)
#   * decimal (most are X.X with one fractional digit)
#   * may contain commas in larger amounts (rare, but seen)
# Cells are whitespace-separated. Empty cells (blank columns) read as
# 0.0 in PDF text extraction — so missing values disappear, not error.
_TOTAL_OPERATING_RE = re.compile(
    r"^\s*Total\s+Operating\s+([0-9,.\-()\s]+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# CAPITAL and SUPPLEMENTAL one-liners. The fiscal year inside the
# parens is informational — we capture the dollar figure regardless.
_CAPITAL_RE = re.compile(
    r"Estimated\s+CAPITAL\s*\(FY\s*\d{4}\)\s*cost\s*:\s*([0-9,.\-()]+)",
    re.IGNORECASE,
)
_SUPPLEMENTAL_RE = re.compile(
    r"Estimated\s+SUPPLEMENTAL\s*\(FY\s*\d{4}\)\s*cost\s*:\s*([0-9,.\-()]+)",
    re.IGNORECASE,
)

# Lone number token: one optional minus, digits with optional commas,
# optional decimal portion. Parenthesized numbers are negatives in
# accounting form (rare in Alaska FNs but harmless to support).
_NUM_RE = re.compile(r"-?\$?\(?-?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d+))?\)?")


def _parse_number(tok: str) -> float:
    """Parse one number token. Returns 0.0 on failure (since blank
    cells should not poison the sum)."""
    if not tok:
        return 0.0
    tok = tok.strip()
    if not tok:
        return 0.0
    neg = False
    # Accounting parens
    if tok.startswith("(") and tok.endswith(")"):
        neg = True
        tok = tok[1:-1].strip()
    # Strip currency symbol and commas
    tok = tok.replace("$", "").replace(",", "")
    try:
        v = float(tok)
    except ValueError:
        return 0.0
    return -v if neg else v


def _parse_number_sequence(s: str) -> list[float]:
    """Pull a list of numbers out of a free-text string (the part of
    a 'Total Operating ...' line after the label). Doesn't enforce a
    column count — that varies slightly across older FN templates."""
    out = []
    for m in _NUM_RE.finditer(s):
        out.append(_parse_number(m.group(0)))
    return out


def parse_fn_amounts(pdf_bytes: bytes) -> dict | None:
    """Parse a single fiscal-note PDF. Returns:
        {
          'operating_by_fy':  {'FY27': 70500.0, 'FY28': 70100.0, ...},
          'operating_total':  388500.0,    # dollars across FY27..FY32
          'capital':          0.0,          # dollars (FY27 capital ask)
          'supplemental':     0.0,          # dollars (FY26 supplemental)
          'total_dollars':    388500.0,
          'fy_start':         27,
          'fy_end':           32,
        }
    or None on any failure.
    """
    try:
        import pypdf  # local import — pypdf is heavy
    except ImportError:
        return None

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return None
        text = reader.pages[0].extract_text() or ""
    except Exception:
        return None

    if not text:
        return None

    # --- Operating: locate the "Total Operating" line on page 1 ---
    m = _TOTAL_OPERATING_RE.search(text)
    if not m:
        # Some pre-2020 FNs use "Total Operating Expenditures" or
        # "Total Operating Costs". Try a looser fallback.
        m = re.search(
            r"Total\s+Operating[A-Za-z\s]*?\n?\s*([0-9,.\-()][0-9,.\-()\s]{4,200})",
            text,
            re.IGNORECASE,
        )
        if not m:
            return None

    nums = _parse_number_sequence(m.group(1))
    if not nums:
        return None

    # Drop the 2nd column ("Included in Governor's Appropriation
    # Request" — almost always 0.0; informational only). The Alaska
    # template prints 7 numeric columns: [Requested FY27, GovFY27,
    # FY28, FY29, FY30, FY31, FY32]. Older FNs may have only 6
    # (no Gov column) or 5 (no FY32). Apply the dedup rule:
    # if the sequence is >= 7 long, drop index 1; otherwise use as-is.
    if len(nums) >= 7:
        op_cols = [nums[0]] + nums[2:7]
    elif len(nums) >= 5:
        op_cols = nums[:5]
    else:
        op_cols = nums

    # Detect the fiscal-year header to set start year. Look for the
    # first "FY 20NN" or "FY20NN" token in the same neighborhood.
    fy_match = re.search(r"FY\s*20(\d{2})", text)
    fy_start = int(fy_match.group(1)) if fy_match else 27
    fy_end = fy_start + len(op_cols) - 1

    operating_by_fy: dict[str, float] = {}
    for i, val in enumerate(op_cols):
        operating_by_fy[f"FY{fy_start + i}"] = val * 1000.0
    operating_total = sum(operating_by_fy.values())

    # --- Capital + supplemental one-liners ---
    capital = 0.0
    cap_m = _CAPITAL_RE.search(text)
    if cap_m:
        capital = _parse_number(cap_m.group(1)) * 1000.0

    supplemental = 0.0
    sup_m = _SUPPLEMENTAL_RE.search(text)
    if sup_m:
        supplemental = _parse_number(sup_m.group(1)) * 1000.0

    total = operating_total + capital + supplemental

    return {
        "operating_by_fy": operating_by_fy,
        "operating_total": operating_total,
        "capital":         capital,
        "supplemental":    supplemental,
        "total_dollars":   total,
        "fy_start":        fy_start,
        "fy_end":          fy_end,
    }


# ---------------------------------------------------------------------------
# Self-test (manual): python -m fiscal_note_parser <PDF_URL>
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys, urllib.request
    if len(sys.argv) != 2:
        print("usage: python -m fiscal_note_parser <url>")
        raise SystemExit(2)
    url = sys.argv[1]
    req = urllib.request.Request(url, headers={"User-Agent": "basis-browser/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    out = parse_fn_amounts(body)
    if out is None:
        print("PARSE FAILED")
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
