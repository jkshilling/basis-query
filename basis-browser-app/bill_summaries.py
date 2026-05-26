"""Hand-authored neutral summaries for awaiting-transmittal bills.

Sponsor statements are advocacy by definition; the legal "An Act
relating to..." titles are precise but cryptic. These summaries
describe what each bill DOES in plain English, without the
sponsor's editorial framing.

Format: keyed by compact billnumber (e.g. "HB 195"). Each entry is
a dict with:
    "summary": short paragraph(s); blank line separates paragraphs.
    "author":  who wrote it (defaults to "editorial" — i.e. this app)
    "session": the session this applies to, so summaries don't leak
               across sessions when a bill number is reused.

When a bill ages out of awaiting-transmittal (gets transmitted /
chaptered / vetoed), its entry can stay here as historical record;
the lookup only fires for currently-displayed bills.
"""

SESSION = "34"

BILL_SUMMARIES = {
    "HB 195": {
        "session": SESSION,
        "summary": (
            "Authorizes Alaska pharmacists to prescribe and administer drugs "
            "under collaborative practice agreements with physicians, "
            "including for COVID-19, influenza, strep, urinary tract "
            "infections, and similar uncomplicated conditions. Establishes "
            "a licensure reciprocity pathway for out-of-state pharmacists.\n\n"
            "Renames “physician assistant” to “physician "
            "associate” throughout state statutes and expands those "
            "professionals' prescribing authority for opioid overdose "
            "reversal drugs. Amends the statutory definition of "
            "“practitioner” accordingly."
        ),
    },
    "HB 221": {
        "session": SESSION,
        "summary": (
            "Designates the first Friday of every October as Alaska Arts "
            "and Culture Day. Non-binding observance — no programs, "
            "appropriations, or regulatory changes. Joins the existing "
            "calendar of state-designated days like Alaska Wild Salmon Day "
            "and Alaska Mining Day."
        ),
    },
    "HB 243": {
        "session": SESSION,
        "summary": (
            "Codifies the Board of Barbers and Hairdressers' authority to "
            "delegate licensing decisions to the Division of Corporations, "
            "Business, and Professional Licensure. Formalizes long-standing "
            "practice that lets the all-volunteer board avoid individually "
            "approving more than 1,000 licenses per year across barbers, "
            "hairdressers, hair braiders, estheticians, manicurists, body "
            "piercers, and tattoo artists."
        ),
    },
    "HB 244": {
        "session": SESSION,
        "summary": (
            "Amends AS 08.68.331 to require that Certified Nurse Aide "
            "training regulations include explicit, demonstrated competency "
            "in specific core skills for safe and effective patient care. "
            "Tightens curriculum standards for CNAs working in hospitals, "
            "long-term care, assisted living, and home health settings; "
            "does not change the certification or licensing process itself."
        ),
    },
    "HB 246": {
        "session": SESSION,
        "summary": (
            "Adjusts the appropriation formula for the Special Education "
            "Service Agency — the state-level body that delivers "
            "specialized services (low-incidence disability supports, "
            "deaf/blind/autism services) to students across Alaska school "
            "districts. Modifies per-student or per-district allocation "
            "amounts; does not change SESA's mandate or eligibility rules."
        ),
    },
    "HB 249": {
        "session": SESSION,
        "summary": (
            "Replaces the notary-acknowledgment requirement with an "
            "electronic-signature option when transferring a vehicle title "
            "to an insurance company after a total-loss claim. Reduces "
            "process friction for rural Alaskans with limited notary "
            "access; aligns with existing State of Alaska and federal "
            "electronic-signature practice."
        ),
    },
    "HB 262": {
        "session": SESSION,
        "summary": (
            "Adds one superior court judge to the Third Judicial District, "
            "to be seated in Palmer. Palmer's four current superior court "
            "judges carry the highest caseloads in the state (683 cases "
            "per judge vs. a 458 statewide average); a fifth judge would "
            "drop the Palmer average to 546. The last new Palmer judgeship "
            "was added in 2006, while the Mat-Su Borough's population has "
            "grown roughly 40% since then."
        ),
    },
    "HB 363": {
        "session": SESSION,
        "summary": (
            "Lets patriotic organizations holding club liquor licenses "
            "(VFW, American Legion, and similar) serve alcoholic beverages "
            "to members of other Alaska-incorporated patriotic-org clubs "
            "with club licenses under AS 04.09.220. Also clarifies the "
            "rules under which such organizations may serve liquor on "
            "premises."
        ),
    },
    "HB 388": {
        "session": SESSION,
        "summary": (
            "Raises the per-borrower cap on loans from the state's Bulk "
            "Fuel Loan Program from $750,000 to $1.5 million and allows "
            "eligible communities to pool resources when participating in "
            "the program. Targeted at rural Alaska communities facing a "
            "narrow shipping window and volatile fuel prices."
        ),
    },
    "SB 104": {
        "session": SESSION,
        "summary": (
            "Creates a transfer-on-death (TOD) designation for motor "
            "vehicles and boats, letting owners name a beneficiary who can "
            "take title at the owner's death without going through "
            "probate. Extends the framework Alaska adopted in 2014 for "
            "real-property TOD deeds.\n\n"
            "Also amends rules governing the transferability of "
            "common-interest-community ownership interests (condominium "
            "and HOA units)."
        ),
    },
    "SB 130": {
        "session": SESSION,
        "summary": (
            "Expands the Fisheries Product Development Tax Credit — a "
            "Fishery Business Tax credit for value-added processing "
            "equipment — to cover all fish and shellfish species "
            "(currently limited to salmon, herring, pollock, sablefish, "
            "and Pacific cod). Adds icing-technology investments to "
            "qualifying activities, requires the Department of Revenue to "
            "issue eligibility determinations faster, and extends the "
            "credit's sunset by 10 years.\n\n"
            "Implements a recommendation of the 2024-2025 Joint "
            "Legislative Task Force Evaluating Alaska's Seafood Industry."
        ),
    },
    "SB 163": {
        "session": SESSION,
        "summary": (
            "Repeals three inactive state special funds: the Public Access "
            "Fund, the Alaska Temporary Assistance Program Emergency "
            "Account, and the 2001 Special Olympics World Winter Games "
            "Reserve Fund. Housekeeping based on a Legislative Finance "
            "Division review of 56 dormant funds; this bill addresses the "
            "subset whose repeal does not require amendments to other "
            "statutory sections."
        ),
    },
    "SB 164": {
        "session": SESSION,
        "summary": (
            "Eliminates several tax-remittance discounts: the timely-filing "
            "credits for motor fuel taxes and tire fees, the tobacco-tax "
            "remittance discount, and the discount paid to wholesalers on "
            "cigarette tax stamps. Implements recommendations from a "
            "Department of Revenue / Legislative Finance Indirect "
            "Expenditure Report review."
        ),
    },
    "SB 174": {
        "session": SESSION,
        "summary": (
            "Establishes the Alaska Invasive Species Council within the "
            "Department of Fish and Game with coordinating authority over "
            "prevention, eradication, and control of invasive species "
            "(northern pike, elodea, orange hawkweed, European green crab, "
            "and others). Adds statutory provisions on invasive-species "
            "management.\n\n"
            "Companion provisions: creates a statewide spay-and-neuter "
            "assistance fund and program, regulates the release of feral "
            "domestic cats, authorizes municipal control of feral cats and "
            "dogs, creates a specialty companion-animal spay/neuter "
            "license plate, and adds a Permanent Fund Dividend contribution "
            "option for the spay/neuter program."
        ),
    },
    "SB 178": {
        "session": SESSION,
        "summary": (
            "Lowers the eligibility threshold for the Alaska Infant "
            "Learning Program from a 50% to a 25% developmental delay, "
            "aligning ILP with the threshold already used for K-12 special "
            "education services. Expands optional Medicaid coverage for "
            "early-intervention therapy services for affected children."
        ),
    },
    "SB 181": {
        "session": SESSION,
        "summary": (
            "Authorizes the Department of Labor and Workforce Development "
            "to share disaggregated employment data with other state "
            "agencies and the University of Alaska under contract "
            "agreement when in the public interest. Removes existing "
            "statutory data-sharing restrictions that the Joint "
            "Legislative Seafood Task Force identified as obstacles to "
            "workforce, fisheries, and education policy analysis."
        ),
    },
    "SB 187": {
        "session": SESSION,
        "summary": (
            "Prohibits seven synthetic food dyes — Red 3, Red 40, "
            "Yellow 5, Yellow 6, Blue 1, Blue 2, and Green 3 — in "
            "meals served by Alaska public schools. Targets dyes commonly "
            "used in cereals, snacks, yogurts, processed meats, dressings, "
            "and canned fruit products. Follows similar legislation "
            "adopted in nine other states."
        ),
    },
    "SB 252": {
        "session": SESSION,
        "summary": (
            "Adopts the 2018 and 2022 Uniform Law Commission amendments to "
            "the Uniform Commercial Code, last updated by Alaska in 2013. "
            "Adds rules governing “controllable electronic records” "
            "(the framework the ULC uses for digital assets including "
            "cryptocurrency) and updates secured-transactions provisions "
            "to accommodate them.\n\n"
            "Also modernizes UCC provisions on sales, negotiable "
            "instruments, letters of credit, warehouse receipts and bills "
            "of lading, investment securities, leases of goods, and fund "
            "transfers — keeping Alaska's commercial code uniform "
            "with the rest of the country."
        ),
    },
}


def get_bill_summary(billnumber, session="34"):
    """Return the neutral summary dict for a bill, or None if no
    hand-authored summary exists yet. Caller decides what to render
    when None (typically: fall back to legal description only)."""
    entry = BILL_SUMMARIES.get((billnumber or "").strip())
    if not entry:
        return None
    if entry.get("session") != session:
        return None
    return entry
