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
#
# The following overrides reflect a political audit through the lens
# of a frustrated GLO Director under a Republican governor whose own
# priorities (besides appropriations) were largely sidelined by a
# Dem-led bipartisan coalition. The strategy distinguishes two
# levers the Governor actually has:
#
#   VETO  — only reserved for bills that are NOT veto-proof, where
#           the Governor can actually kill the bill. Used here for
#           new taxes and anti-business restrictions that aren't
#           veto-proof.
#
#   LWOS  — for veto-proof Dem-priority bills the Governor disagrees
#           with but where an override is automatic. LWOS lets the
#           bill become law without his signature, signaling
#           disagreement without forcing the override-vote theater.
#
GLO_OVERRIDES: dict[str, str | dict] = {

    # ── SIGN → VETO (non-veto-proof, real veto opportunity) ───────
    "SB 24": {
        "rec": "VETO",
        "note": ("New e-cigarette tax; not veto-proof (39 combined yeas). "
                 "Vetoing aligns with no-new-taxes orthodoxy and signals "
                 "consistent fiscal-conservative posture."),
    },
    "HB 280": {
        "rec": "VETO",
        "note": ("Tax-apportionment bill; not veto-proof (39 combined "
                 "yeas). Real opportunity to kill a tax change the "
                 "administration didn't request."),
    },

    # ── LWOS → VETO (genuine kill, not veto-proof) ────────────────
    "HB 25": {
        "rec": "VETO",
        "note": ("Disposable food-service-ware restriction; not "
                 "veto-proof (38 combined yeas). Anti-business "
                 "restriction the administration can kill outright."),
    },

    # ── SIGN → LWOS (veto-proof Dem priorities the Gov doesn't own)
    "HB 16": {
        "rec": "LWOS",
        "note": ("Dem-priority campaign-finance restriction. R "
                 "governors rarely endorse new contribution limits; "
                 "LWOS signals disagreement without forcing override "
                 "theater."),
    },
    "HB 23": {
        "rec": "LWOS",
        "note": ("Restores a state Civil Rights Commission the "
                 "administration previously declined to fund. LWOS "
                 "distances the Governor from a Dem-priority "
                 "infrastructure decision he didn't drive."),
    },
    "HB 110": {
        "rec": "LWOS",
        "note": ("Expands interstate licensure compacts and state "
                 "social-work regulatory infrastructure. Bipartisan "
                 "support but Dem-sponsored; LWOS signals caution on "
                 "scope-of-government expansion."),
    },
    "HB 173": {
        "rec": "LWOS",
        "note": ("Interstate compacts for OT/PT/audiology + scope-of-"
                 "practice changes. Adds regulatory infrastructure; "
                 "LWOS expresses business-friendly skepticism."),
    },
    "HB 176": {
        "rec": "LWOS",
        "note": ("University of Alaska itself opposed this bill in "
                 "its own internal briefing (Hutchison email). LWOS "
                 "defers to UA's objection without provoking an "
                 "override vote."),
    },
    "HB 195": {
        "rec": "LWOS",
        "note": ("Pharmacist scope-of-practice expansion; D-sponsored. "
                 "LWOS expresses caution about expanding non-physician "
                 "prescriptive authority."),
    },
    "HB 298": {
        "rec": "LWOS",
        "note": ("Legislators rewriting their own ethics-committee "
                 "rules. Not the Governor's place to endorse a "
                 "co-equal branch's internal procedural reform; LWOS."),
    },
    "HB 302": {
        "rec": "LWOS",
        "note": ("Expands unemployment-insurance benefits. R "
                 "governors don't ordinarily expand UI; LWOS signals "
                 "fiscal-conservative disagreement."),
    },
    "SB 21": {
        "rec": "LWOS",
        "note": ("Creates a new state-administered savings program "
                 "and PFD investment account — government expansion "
                 "the administration didn't request. LWOS signals "
                 "small-government caution."),
    },
    "SB 41": {
        "rec": "LWOS",
        "note": ("New mental-health-education curriculum mandate on "
                 "local school boards. R governors typically resist "
                 "state curriculum mandates; LWOS signals "
                 "local-control posture."),
    },
    "SB 79": {
        "rec": "LWOS",
        "note": ("New employer regulations on payroll-card practices "
                 "(free withdrawals, FDIC insurance, fee bans). "
                 "Labor regulation the administration didn't drive; "
                 "LWOS signals business-friendly disagreement."),
    },
    "SB 167": {
        "rec": "LWOS",
        "note": ("Expands PFD eligibility for overturned-conviction "
                 "claimants; Dem-priority. LWOS signals "
                 "fiscal-conservative disagreement on PFD expansion."),
    },
    "SB 178": {
        "rec": "LWOS",
        "note": ("Expands Early Intervention Services entitlement "
                 "(~$60M projected fiscal cost). LWOS signals "
                 "fiscal-conservative caution on entitlement growth."),
    },
    "SB 272": {
        "rec": "LWOS",
        "note": ("New statewide health-information-exchange "
                 "infrastructure. LWOS signals caution on building "
                 "state-run health data systems."),
    },

    # ── LWOS → VETO (escalate from passive to active opposition) ──
    "SB 143": {
        "rec": "VETO",
        "note": ("School-board terms / training / city-council reforms "
                 "are local-governance intrusions. Veto signals "
                 "commitment to local-control principles even though "
                 "the bill is veto-proof."),
    },
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
