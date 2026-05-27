"""GLO (Governor's Legislative Office) recommendation per bill.

The GLO sits inside the Office of the Governor and aggregates each
department's blue sheet into a single recommendation that goes to
the Governor. We don't have the GLO's internal documents, so we
compute a best-guess from observable signals:

  1. Governor's own bills (Requestor = "THE GOVERNOR") → SIGN.
  2. All departmental blue sheets agree → use that recommendation.
  3. Any blue sheet says VETO → VETO carries (most conservative).
  4. Otherwise majority of blue sheets.
  5. No blue sheets on file → unknown ("?").

Manual overrides live in GLO_OVERRIDES below. Each entry can be
either a string ("SIGN"/"VETO"/"LWOS") or a dict with a 'rec' and
optional 'note' that displays as the tooltip. The override always
wins over the guess.
"""

from __future__ import annotations

# {billnumber: "SIGN" | "VETO" | "LWOS"}  or
# {billnumber: {"rec": "SIGN", "note": "Why I disagreed with the guess"}}
GLO_OVERRIDES: dict[str, str | dict] = {
    # Example:
    # "HB 110": {"rec": "SIGN", "note": "Federal funding tied to passage"},
}


def _normalize(value):
    if isinstance(value, str):
        return {"rec": value.upper(), "note": ""}
    if isinstance(value, dict):
        return {
            "rec": (value.get("rec") or "").upper(),
            "note": value.get("note") or "",
        }
    return {"rec": "", "note": ""}


def guess(blue_sheets, requestor="", veto_proof=False, billnumber=""):
    """Compute the GLO recommendation for one bill.

    Args:
        blue_sheets: list of blue-sheet dicts from blue_sheets.index().
                     Each has 'agency' + 'recommendation' (may be '').
        requestor: BASIS Requestor value (e.g. 'THE GOVERNOR').
        veto_proof: bool — did the bill pass with a 2/3 supermajority?
        billnumber: e.g. 'HB 110' — for override lookup.

    Returns dict with keys:
        rec      : "SIGN" | "VETO" | "LWOS" | "" (empty when unknown)
        source   : "override" | "departments" | "governor-bill" | "veto-proof" | ""
        note     : short human-readable explanation for the tooltip
        overridden: True when a manual override is in effect (so the
                    UI can flag it visually).
    """
    bn = (billnumber or "").strip()
    ov = _normalize(GLO_OVERRIDES.get(bn))
    if ov["rec"]:
        return {
            "rec": ov["rec"],
            "source": "override",
            "note": ov["note"] or "Manually overridden",
            "overridden": True,
        }

    req = (requestor or "").upper()
    if "GOVERNOR" in req:
        return {
            "rec": "SIGN",
            "source": "governor-bill",
            "note": "Governor's own bill (by request of the Governor)",
            "overridden": False,
        }

    recs = [s.get("recommendation", "") for s in (blue_sheets or [])
            if s.get("recommendation")]

    if recs:
        # Conservative roll-up: a single VETO recommendation outweighs
        # multiple SIGNs (politically, a department's veto rec gives
        # cover for a veto). LWOS sits in between.
        n_veto = sum(1 for r in recs if r == "VETO")
        n_lwos = sum(1 for r in recs if r == "LWOS")
        n_sign = sum(1 for r in recs if r == "SIGN")
        if n_veto:
            return {
                "rec": "VETO",
                "source": "departments",
                "note": (f"{n_veto} of {len(recs)} department"
                         f"{'s' if len(recs) != 1 else ''} recommend veto"),
                "overridden": False,
            }
        if n_lwos and n_lwos >= n_sign:
            return {
                "rec": "LWOS",
                "source": "departments",
                "note": (f"{n_lwos} of {len(recs)} department"
                         f"{'s' if len(recs) != 1 else ''} recommend LWOS"),
                "overridden": False,
            }
        return {
            "rec": "SIGN",
            "source": "departments",
            "note": (f"{n_sign} of {len(recs)} department"
                     f"{'s' if len(recs) != 1 else ''} recommend signing"),
            "overridden": False,
        }

    # No blue sheets on file. Veto-proof passage is a strong signal
    # the Governor should just sign (a veto would likely be overridden,
    # making it political theater that bruises relationships).
    if veto_proof:
        return {
            "rec": "SIGN",
            "source": "veto-proof",
            "note": "Veto-proof supermajority; a veto would likely be overridden",
            "overridden": False,
        }

    return {
        "rec": "",
        "source": "",
        "note": "No departmental blue sheets on file yet",
        "overridden": False,
    }
