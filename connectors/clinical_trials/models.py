"""
connectors/clinical_trials/models.py

ClinicalTrialRecord — the normalized registry record — plus the verified enumerations and the
evidence-safety classification rules that must travel WITH the data rather than living only in
prose.

Every enum value below was read from the live `GET /api/v2/studies/enums` endpoint on 2026-08-31
(see CLINICALTRIALS_API_V2_VERIFICATION.md §6), not from memory or documentation.
"""
import re
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

# ---------------------------------------------------------------------------
# Verified enumerations
# ---------------------------------------------------------------------------
OVERALL_STATUSES = (
    "ACTIVE_NOT_RECRUITING", "COMPLETED", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING",
    "RECRUITING", "SUSPENDED", "TERMINATED", "WITHDRAWN", "AVAILABLE", "NO_LONGER_AVAILABLE",
    "TEMPORARILY_NOT_AVAILABLE", "APPROVED_FOR_MARKETING", "WITHHELD", "UNKNOWN",
)
STUDY_TYPES = ("EXPANDED_ACCESS", "INTERVENTIONAL", "OBSERVATIONAL")
PHASES = ("NA", "EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4")
REFERENCE_TYPES = ("BACKGROUND", "RESULT", "DERIVED")

# ---------------------------------------------------------------------------
# NCT ID validation — Section 3 of the Phase B brief
# ---------------------------------------------------------------------------
NCT_ID_PATTERN = re.compile(r"^NCT\d{8}$")

NCT_NORMALIZATION_POLICY_NOTE = (
    "An NCT ID is validated, never repaired. Whitespace and case are the ONLY things normalized "
    "(' nct00782171 ' -> 'NCT00782171'), because those do not change which trial is designated. "
    "Anything else — padding a short number with zeros, stripping extra digits, correcting a "
    "typo — would silently designate a DIFFERENT, possibly real, trial. Such input is rejected "
    "with IDENTIFIER_INVALID and never sent to the network."
)


def validate_nct_id(raw):
    """
    Returns a canonical NCT ID string, or None if the input is not a valid NCT ID.

    Case-insensitive and whitespace-tolerant, because neither changes trial identity. Everything
    else is a rejection — this function NEVER invents or repairs an ID into a valid-looking one
    (NCT_NORMALIZATION_POLICY_NOTE). 'NCT123' does not become 'NCT00000123'.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if NCT_ID_PATTERN.match(s):
        return s
    return None


# ---------------------------------------------------------------------------
# Evidence-safety classification — Sections 5, 6 and 9 of the Phase B brief
# ---------------------------------------------------------------------------
EVIDENCE_CLASS_NO_RESULTS = "REGISTERED_NO_RESULTS"
EVIDENCE_CLASS_REGISTRY_RESULTS = "REGISTERED_REGISTRY_RESULTS_POSTED"
EVIDENCE_CLASS_LINKED_PUBLICATION = "REGISTERED_LINKED_PUBLICATION"

EVIDENCE_CLASS_MEANINGS = {
    EVIDENCE_CLASS_NO_RESULTS: (
        "A. Registered trial, no posted results. Carries NO efficacy information whatsoever. "
        "Registration is a statement of intent, not a finding."
    ),
    EVIDENCE_CLASS_REGISTRY_RESULTS: (
        "B. Registered trial with results posted to ClinicalTrials.gov. These are "
        "sponsor-submitted, structured, and NOT peer-reviewed. Label as registry-reported "
        "results; never assign peer-reviewed weight."
    ),
    EVIDENCE_CLASS_LINKED_PUBLICATION: (
        "C. Registered trial with a linked publication reference. The publication itself must be "
        "retrieved and appraised through PubMed/Crossref before it counts as published evidence — "
        "the registry's reference alone is a pointer, not the evidence."
    ),
}

# Registry status is NOT evidence quality. This mapping exists so the rule is enforceable rather
# than merely documented: no status maps to any efficacy conclusion.
STATUS_SAFETY_NOTES = {
    "WITHDRAWN": (
        "TRIAL NEVER STARTED (withdrawn before enrolment). Must NOT be presented as treatment "
        "evidence of any kind, and withdrawal is NOT a negative result about the intervention."
    ),
    "TERMINATED": (
        "TRIAL STOPPED EARLY. State the termination and its stated reason where available. "
        "Termination is not by itself evidence the intervention failed — trials terminate for "
        "recruitment, funding and logistical reasons."
    ),
    "SUSPENDED": "TRIAL SUSPENDED. Flag explicitly; status may change.",
    "COMPLETED": (
        "COMPLETED MEANS THE TRIAL FINISHED, NOT THAT IT SUCCEEDED. Completion says nothing about "
        "the direction or significance of any outcome. Do not infer benefit."
    ),
    "UNKNOWN": (
        "STATUS UNKNOWN — the sponsor has not verified status recently. Preserve the uncertainty; "
        "do not assume the trial completed, or that it did not."
    ),
    "RECRUITING": "ONGOING, enrolling. No outcome information is available from status alone.",
    "NOT_YET_RECRUITING": "NOT STARTED. No outcome information available.",
    "ACTIVE_NOT_RECRUITING": "ONGOING, enrolment closed. No outcome information from status alone.",
    "ENROLLING_BY_INVITATION": "ONGOING, restricted enrolment. No outcome information from status alone.",
    "WITHHELD": "RECORD WITHHELD by the registry. Treat as uninformative, not as absence.",
}

NO_STATUS_IMPLIES_EFFICACY_NOTE = (
    "No value of overall_status, on its own, licenses any statement about whether an intervention "
    "works. Efficacy claims require reported outcome data — registry-posted results (labelled as "
    "such) or an appraised publication."
)


@dataclass
class ClinicalTrialRecord:
    """
    Normalized registry record. Every field defaults to None/empty and is populated ONLY from
    data actually present in the API response — a missing field stays missing. Nothing here is
    inferred, defaulted to a plausible value, or back-filled.
    """
    nct_id: Optional[str] = None
    brief_title: Optional[str] = None
    official_title: Optional[str] = None
    overall_status: Optional[str] = None
    why_stopped: Optional[str] = None
    study_type: Optional[str] = None
    phases: Optional[List[str]] = None
    enrollment: Optional[int] = None
    enrollment_type: Optional[str] = None
    start_date: Optional[str] = None
    primary_completion_date: Optional[str] = None
    completion_date: Optional[str] = None
    conditions: Optional[List[str]] = None
    interventions: Optional[List[Dict[str, Any]]] = None
    intervention_types: Optional[List[str]] = None
    primary_outcomes: Optional[List[Dict[str, Any]]] = None
    secondary_outcomes: Optional[List[Dict[str, Any]]] = None
    eligibility_criteria: Optional[str] = None
    sex: Optional[str] = None
    minimum_age: Optional[str] = None
    maximum_age: Optional[str] = None
    healthy_volunteers: Optional[bool] = None
    locations: Optional[List[Dict[str, Any]]] = None
    lead_sponsor: Optional[Dict[str, Any]] = None
    collaborators: Optional[List[Dict[str, Any]]] = None
    responsible_party: Optional[Dict[str, Any]] = None
    has_results: Optional[bool] = None
    results_first_post_date: Optional[str] = None
    study_first_post_date: Optional[str] = None
    last_update_post_date: Optional[str] = None
    central_contacts: Optional[List[Dict[str, Any]]] = None

    # Publication linkage (Section 7) — pointers only, never treated as retrieved evidence.
    publication_references: Optional[List[Dict[str, Any]]] = None

    # Registry-reported results (Section 6/10) — captured SEPARATELY, never merged into a
    # publication-grade appraisal.
    registry_results: Optional[Dict[str, Any]] = None

    # Evidence-safety annotations (Sections 5/6/9) — carried with the record so a downstream
    # consumer cannot lose them.
    evidence_class: Optional[str] = None
    evidence_class_note: Optional[str] = None
    status_safety_note: Optional[str] = None

    # Provenance (Section 11)
    source_connector: str = "clinical_trials"
    source_database: str = "ClinicalTrials.gov"
    retrieved_at: Optional[str] = None
    query: Optional[str] = None
    retrieval_status: Optional[str] = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def classify_evidence(has_results, publication_references):
    """
    Section 6 — results-aware classification. Returns (evidence_class, note).

    Distinguishes A (registered only), B (registry results posted) and C (linked publication).
    Category D (a publication retrieved independently through PubMed) is deliberately NOT
    assignable here: it is a property of a PubMed record, not of a registry record, and
    collapsing the two is exactly the conflation Section 6 forbids.

    A record can be both B and C. Ordering is deliberate: a linked publication is the stronger
    pointer, so C wins the label, but has_results stays true on the record and the registry
    results remain captured separately — the label never erases the underlying fields.
    """
    refs = publication_references or []
    # Only RESULT and DERIVED references point at a report OF THIS TRIAL. BACKGROUND is cited
    # background literature and asserts nothing about this trial's outcomes.
    reporting_refs = [r for r in refs if r.get("type") in ("RESULT", "DERIVED")]
    if reporting_refs:
        return EVIDENCE_CLASS_LINKED_PUBLICATION, EVIDENCE_CLASS_MEANINGS[EVIDENCE_CLASS_LINKED_PUBLICATION]
    if has_results is True:
        return EVIDENCE_CLASS_REGISTRY_RESULTS, EVIDENCE_CLASS_MEANINGS[EVIDENCE_CLASS_REGISTRY_RESULTS]
    return EVIDENCE_CLASS_NO_RESULTS, EVIDENCE_CLASS_MEANINGS[EVIDENCE_CLASS_NO_RESULTS]


def status_safety_note(overall_status):
    """Section 9. Returns the mandatory caution for a status, or a conservative default."""
    if not overall_status:
        return "STATUS NOT REPORTED — preserve the uncertainty; infer nothing."
    return STATUS_SAFETY_NOTES.get(
        overall_status,
        f"Status {overall_status} — registry status only; it carries no efficacy information.",
    )


def build_status_filter(statuses):
    """
    Build the verified `filter.overallStatus` value: a single status or several joined by '|'
    (verified as OR, CLINICALTRIALS_API_V2_VERIFICATION.md §2).

    Unrecognised values are DROPPED rather than passed through — the API rejects an unknown enum
    with HTTP 400 (verified), and silently sending one would turn a caller's typo into a total
    query failure. Returns None if nothing valid remains.
    """
    if not statuses:
        return None
    if isinstance(statuses, str):
        statuses = [statuses]
    valid = [s.strip().upper() for s in statuses if s and s.strip().upper() in OVERALL_STATUSES]
    return "|".join(valid) if valid else None


def build_phase_filter(phases):
    """
    Phase filtering uses the Essie advanced-filter syntax (`filter.advanced=AREA[Phase]PHASE3`).
    Returns None if no valid phase is given, for the same reason as build_status_filter.
    """
    if not phases:
        return None
    if isinstance(phases, str):
        phases = [phases]
    valid = [p.strip().upper() for p in phases if p and p.strip().upper() in PHASES]
    if not valid:
        return None
    if len(valid) == 1:
        return f"AREA[Phase]{valid[0]}"
    return "(" + " OR ".join(f"AREA[Phase]{p}" for p in valid) + ")"
