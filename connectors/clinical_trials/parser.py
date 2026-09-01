"""
connectors/clinical_trials/parser.py

Parses ClinicalTrials.gov API v2 JSON into ClinicalTrialRecord.

Two structural facts about the API drive the whole design here, both verified live
(CLINICALTRIALS_API_V2_VERIFICATION.md §5):

1. Modules are OMITTED, not nulled, when absent. NCT00000102 has no outcomesModule and no
   referencesModule at all. Every accessor therefore tolerates absence and yields None, never a
   KeyError and never a fabricated default.
2. Dates and enrolment are STRUCTS carrying an ACTUAL/ESTIMATED type. That distinction is
   clinically material (an estimated completion date is a plan; an actual one is a fact), so it
   is preserved rather than flattened to a bare string.

No value in this module is ever inferred, defaulted to something plausible, or computed. If the
registry did not say it, the field stays None.
"""
import json

from models import (
    ClinicalTrialRecord, classify_evidence, status_safety_note, validate_nct_id,
)
from errors import (
    ClinicalTrialsConnectorError, STATUS_PARSE_ERROR,
)


def _mod(protocol, name):
    """A protocolSection module, or {} when the module is absent entirely."""
    return (protocol or {}).get(name) or {}


def _date(struct):
    """
    Extract the date string from a {"date": ..., "type": ACTUAL|ESTIMATED} struct.
    Returns None when the struct is missing. The type is preserved separately by _date_type.
    """
    if not isinstance(struct, dict):
        return None
    return struct.get("date")


def _date_type(struct):
    if not isinstance(struct, dict):
        return None
    return struct.get("type")


def parse_study(study, query=None, retrieved_at=None, retrieval_status=None):
    """
    Parse one API v2 study object (as returned inside `studies[]` or as a bare study detail)
    into a ClinicalTrialRecord dict.

    Raises ClinicalTrialsConnectorError(PARSE_ERROR) only when the payload is not a study at all
    — a study without an NCT ID is not a usable record and must not be silently emitted with a
    null identity.
    """
    if not isinstance(study, dict):
        raise ClinicalTrialsConnectorError(STATUS_PARSE_ERROR, "Study payload is not an object")

    protocol = study.get("protocolSection")
    if not isinstance(protocol, dict):
        raise ClinicalTrialsConnectorError(STATUS_PARSE_ERROR, "Study has no protocolSection")

    ident = _mod(protocol, "identificationModule")
    nct_id = validate_nct_id(ident.get("nctId"))
    if not nct_id:
        raise ClinicalTrialsConnectorError(
            STATUS_PARSE_ERROR,
            f"Study has missing or invalid nctId: {ident.get('nctId')!r}",
        )

    status_mod = _mod(protocol, "statusModule")
    design = _mod(protocol, "designModule")
    arms = _mod(protocol, "armsInterventionsModule")
    outcomes = _mod(protocol, "outcomesModule")
    elig = _mod(protocol, "eligibilityModule")
    contacts = _mod(protocol, "contactsLocationsModule")
    sponsors = _mod(protocol, "sponsorCollaboratorsModule")
    conditions_mod = _mod(protocol, "conditionsModule")
    refs_mod = _mod(protocol, "referencesModule")

    enrollment_info = design.get("enrollmentInfo") or {}
    interventions = arms.get("interventions") or []

    # hasResults is a top-level boolean and is always present in a study detail response; in a
    # search response it may be absent, in which case it stays None rather than becoming False —
    # "not stated" and "no results" are different facts.
    has_results = study.get("hasResults")

    publication_references = _parse_references(refs_mod)
    registry_results = _parse_registry_results(study)

    evidence_class, evidence_note = classify_evidence(has_results, publication_references)
    overall_status = status_mod.get("overallStatus")

    record = ClinicalTrialRecord(
        nct_id=nct_id,
        brief_title=ident.get("briefTitle"),
        official_title=ident.get("officialTitle"),
        overall_status=overall_status,
        why_stopped=status_mod.get("whyStopped"),
        study_type=design.get("studyType"),
        phases=design.get("phases") or None,
        enrollment=enrollment_info.get("count"),
        enrollment_type=enrollment_info.get("type"),
        start_date=_date(status_mod.get("startDateStruct")),
        primary_completion_date=_date(status_mod.get("primaryCompletionDateStruct")),
        completion_date=_date(status_mod.get("completionDateStruct")),
        conditions=conditions_mod.get("conditions") or None,
        interventions=[
            {
                "type": i.get("type"),
                "name": i.get("name"),
                "description": i.get("description"),
                "other_names": i.get("otherNames"),
            }
            for i in interventions
        ] or None,
        intervention_types=sorted({i.get("type") for i in interventions if i.get("type")}) or None,
        primary_outcomes=outcomes.get("primaryOutcomes") or None,
        secondary_outcomes=outcomes.get("secondaryOutcomes") or None,
        eligibility_criteria=elig.get("eligibilityCriteria"),
        sex=elig.get("sex"),
        minimum_age=elig.get("minimumAge"),
        maximum_age=elig.get("maximumAge"),
        healthy_volunteers=elig.get("healthyVolunteers"),
        locations=contacts.get("locations") or None,
        lead_sponsor=sponsors.get("leadSponsor"),
        collaborators=sponsors.get("collaborators") or None,
        responsible_party=sponsors.get("responsibleParty"),
        has_results=has_results,
        results_first_post_date=_date(status_mod.get("resultsFirstPostDateStruct")),
        study_first_post_date=_date(status_mod.get("studyFirstPostDateStruct")),
        last_update_post_date=_date(status_mod.get("lastUpdatePostDateStruct")),
        central_contacts=contacts.get("centralContacts") or None,
        publication_references=publication_references,
        registry_results=registry_results,
        evidence_class=evidence_class,
        evidence_class_note=evidence_note,
        status_safety_note=status_safety_note(overall_status),
        retrieved_at=retrieved_at,
        query=query,
        retrieval_status=retrieval_status,
    )
    d = record.to_dict()
    # Date-struct types preserved alongside the dates (ACTUAL vs ESTIMATED is material).
    d["date_types"] = {
        "start_date": _date_type(status_mod.get("startDateStruct")),
        "primary_completion_date": _date_type(status_mod.get("primaryCompletionDateStruct")),
        "completion_date": _date_type(status_mod.get("completionDateStruct")),
    }
    return d


def _parse_references(refs_mod):
    """
    Parse protocolSection.referencesModule.references[] = {pmid, type, citation}.

    The `type` is preserved verbatim and is load-bearing (verified enum: BACKGROUND / RESULT /
    DERIVED). Only RESULT and DERIVED report on THIS trial; BACKGROUND is cited background
    literature. Nothing is filtered out here — the classification happens in models.classify_
    evidence and linkage.py, so a caller can always see every reference the registry actually
    published.
    """
    refs = (refs_mod or {}).get("references") or []
    out = []
    for r in refs:
        if not isinstance(r, dict):
            continue
        pmid = r.get("pmid")
        out.append({
            "pmid": str(pmid) if pmid else None,
            "type": r.get("type"),
            "citation": r.get("citation"),
            # Explicit, so no downstream consumer has to re-derive the rule.
            "reports_this_trial": r.get("type") in ("RESULT", "DERIVED"),
        })
    return out or None


def _parse_registry_results(study):
    """
    Section 10 — registry-reported results.

    Extracts ONLY what the registry structurally reports: participant flow, baseline
    characteristics, outcome measures, adverse events. Nothing is calculated, no significance is
    inferred, no summary statistic is derived. The raw module content is carried through so a
    later appraisal step can read it, with a mandatory label attached so it cannot be mistaken
    for a peer-reviewed finding.
    """
    results = study.get("resultsSection")
    if not isinstance(results, dict) or not results:
        return None

    flow = results.get("participantFlowModule") or {}
    baseline = results.get("baselineCharacteristicsModule") or {}
    outcome_measures = (results.get("outcomeMeasuresModule") or {}).get("outcomeMeasures") or []
    adverse = results.get("adverseEventsModule") or {}

    return {
        "label": "REGISTRY-REPORTED RESULTS — sponsor-submitted, NOT peer-reviewed",
        "interpretation_rule": (
            "Structured values as reported by the sponsor to ClinicalTrials.gov. No statistical "
            "significance, effect size, or direction of benefit may be inferred beyond what is "
            "explicitly reported here. These do not carry peer-reviewed evidentiary weight."
        ),
        "participant_flow": flow or None,
        "baseline_characteristics": baseline or None,
        "outcome_measures": outcome_measures or None,
        "adverse_events": adverse or None,
        "outcome_measure_count": len(outcome_measures),
        "has_adverse_event_data": bool(adverse.get("eventGroups")),
    }


def parse_search_response(text, query=None, retrieved_at=None, retrieval_status=None):
    """
    Parse a /studies search response into (records, total_count, next_page_token).

    Envelope verified live: {"totalCount": int (only with countTotal=true), "studies": [...],
    "nextPageToken": str}.
    """
    try:
        payload = json.loads(text)
    except Exception as exc:
        raise ClinicalTrialsConnectorError(STATUS_PARSE_ERROR, f"Response is not valid JSON: {exc}")

    if not isinstance(payload, dict):
        raise ClinicalTrialsConnectorError(STATUS_PARSE_ERROR, "Response JSON is not an object")

    studies = payload.get("studies")
    if studies is None:
        raise ClinicalTrialsConnectorError(
            STATUS_PARSE_ERROR, "Response JSON has no 'studies' key")
    if not isinstance(studies, list):
        raise ClinicalTrialsConnectorError(STATUS_PARSE_ERROR, "'studies' is not a list")

    records = [
        parse_study(s, query=query, retrieved_at=retrieved_at, retrieval_status=retrieval_status)
        for s in studies
    ]
    return records, payload.get("totalCount"), payload.get("nextPageToken")


def parse_study_response(text, query=None, retrieved_at=None, retrieval_status=None):
    """Parse a /studies/{nctId} detail response into a single record dict."""
    try:
        payload = json.loads(text)
    except Exception as exc:
        raise ClinicalTrialsConnectorError(STATUS_PARSE_ERROR, f"Response is not valid JSON: {exc}")
    return parse_study(payload, query=query, retrieved_at=retrieved_at,
                        retrieval_status=retrieval_status)
