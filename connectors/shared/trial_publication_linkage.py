"""
connectors/shared/trial_publication_linkage.py

Sections 7 and 8 of Phase B: trial <-> publication linkage, and the anti-double-counting rule.

Lives in shared/ rather than clinical_trials/ because it spans connectors — it reasons about a
ClinicalTrials.gov record and a PubMed record together. Executable, so the rule is enforced
rather than merely written down (same reasoning as citation_verifier.py and retraction_gate.py).

THE TWO RULES THIS MODULE ENFORCES

1. A linkage is asserted ONLY on a real identifier. LINK_VERIFIED requires an NCT ID present in
   the registry's own reference list, or an NCT ID found in the publication's own metadata, that
   matches. Similar titles, same authors, same year, same topic — none of these establish a link,
   and none of them ever produce LINK_VERIFIED here.

2. A trial and its publication are ONE study, not two. They are linked, never counted twice.
"""
import re

LINK_VERIFIED = "TRIAL ↔ PUBLICATION LINK VERIFIED"
LINK_UNVERIFIED = "TRIAL ↔ PUBLICATION LINK UNVERIFIED"
LINK_MISMATCH = "TRIAL ↔ PUBLICATION LINK MISMATCH"

# An NCT ID as it appears inside free text (abstract, secondary ID, publication metadata).
NCT_IN_TEXT = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)

BASIS_REGISTRY_REFERENCE = "registry_reference_pmid"
BASIS_PUBLICATION_NCT = "publication_metadata_nct_id"
BASIS_BOTH = "registry_reference_and_publication_metadata"


def extract_nct_ids_from_text(*texts):
    """Return the set of canonical NCT IDs appearing literally in any of the given strings."""
    found = set()
    for t in texts:
        if not t:
            continue
        for m in NCT_IN_TEXT.findall(str(t)):
            found.add(m.upper())
    return found


def link_trial_to_publication(trial_record, pubmed_record):
    """
    Determine whether a ClinicalTrials.gov record and a PubMed record describe the same trial.

    Returns:
        {status, basis, nct_id, pmid, reasons, same_underlying_study}

    status is LINK_VERIFIED only when a real identifier establishes the relationship in at least
    one direction:
      - the registry's referencesModule lists this PMID with type RESULT or DERIVED, OR
      - the publication's own metadata contains this trial's NCT ID.
    A BACKGROUND reference NEVER verifies a link — it is cited background literature, not a
    report of this trial.

    LINK_MISMATCH is returned when the publication names a DIFFERENT trial's NCT ID and nothing
    links it to this one — a distinct and important outcome from "no evidence of a link".
    """
    reasons = []
    nct_id = (trial_record or {}).get("nct_id")
    pmid = str((pubmed_record or {}).get("pmid") or "") or None

    if not nct_id or not pmid:
        return {
            "status": LINK_UNVERIFIED,
            "basis": None,
            "nct_id": nct_id,
            "pmid": pmid,
            "reasons": ["Missing NCT ID or PMID — no identifier available to establish a link."],
            "same_underlying_study": False,
        }

    # Direction 1: the registry itself points at this PMID.
    registry_pmids = {
        r.get("pmid") for r in (trial_record.get("publication_references") or [])
        if r.get("pmid") and r.get("reports_this_trial")
    }
    background_pmids = {
        r.get("pmid") for r in (trial_record.get("publication_references") or [])
        if r.get("pmid") and not r.get("reports_this_trial")
    }
    registry_side = pmid in registry_pmids

    # Direction 2: the publication's own metadata names an NCT ID.
    pub_ncts = extract_nct_ids_from_text(
        pubmed_record.get("abstract"),
        pubmed_record.get("title"),
        " ".join(pubmed_record.get("secondary_ids") or []) if pubmed_record.get("secondary_ids") else None,
        " ".join(pubmed_record.get("databank_accessions") or []) if pubmed_record.get("databank_accessions") else None,
    )
    publication_side = nct_id in pub_ncts

    if registry_side:
        reasons.append(f"Registry reference list names PMID {pmid} as a report of {nct_id}.")
    if publication_side:
        reasons.append(f"Publication metadata contains {nct_id}.")

    if registry_side and publication_side:
        basis = BASIS_BOTH
    elif registry_side:
        basis = BASIS_REGISTRY_REFERENCE
    elif publication_side:
        basis = BASIS_PUBLICATION_NCT
    else:
        basis = None

    if basis:
        return {
            "status": LINK_VERIFIED, "basis": basis, "nct_id": nct_id, "pmid": pmid,
            "reasons": reasons, "same_underlying_study": True,
        }

    if pmid in background_pmids:
        reasons.append(
            f"PMID {pmid} appears in the registry's references but with type BACKGROUND — cited "
            "background literature, not a report of this trial. This does NOT verify a link.")

    if pub_ncts:
        reasons.append(
            f"Publication names a different trial: {sorted(pub_ncts)} — not {nct_id}.")
        return {
            "status": LINK_MISMATCH, "basis": None, "nct_id": nct_id, "pmid": pmid,
            "reasons": reasons, "same_underlying_study": False,
        }

    reasons.append(
        "No identifier links these records. Topic, title, author or year similarity is NOT a "
        "basis for asserting a trial-publication link.")
    return {
        "status": LINK_UNVERIFIED, "basis": None, "nct_id": nct_id, "pmid": pmid,
        "reasons": reasons, "same_underlying_study": False,
    }


def deduplicate_trials_and_publications(trial_records, publication_records):
    """
    Section 8. A registry record and its publication are two records describing ONE trial and must
    never be counted as two independent studies.

    Returns:
        {studies: [...], independent_study_count, linked_pairs, unlinked_trials,
         unlinked_publications}

    Each entry in `studies` is one underlying study, with `identity` (the NCT ID where available)
    and the constituent records attached. `independent_study_count` is the number a synthesis step
    may legitimately cite — never the sum of the two input list lengths when links exist.

    Unlinked records are NOT merged on suspicion. A publication with no NCT identifier stays its
    own study; guessing would be exactly the fabrication Section 7 forbids.
    """
    trial_records = list(trial_records or [])
    publication_records = list(publication_records or [])

    studies = []
    linked_pairs = []
    used_pubs = set()

    for trial in trial_records:
        entry = {
            "identity_type": "nct_id" if trial.get("nct_id") else None,
            "identity": trial.get("nct_id"),
            "trial_record": trial,
            "publication_records": [],
            "link_details": [],
        }
        for idx, pub in enumerate(publication_records):
            link = link_trial_to_publication(trial, pub)
            if link["same_underlying_study"]:
                entry["publication_records"].append(pub)
                entry["link_details"].append(link)
                linked_pairs.append(link)
                used_pubs.add(idx)
        entry["record_count"] = 1 + len(entry["publication_records"])
        entry["counts_as_studies"] = 1  # the invariant: one underlying trial, one study
        studies.append(entry)

    unlinked_publications = []
    for idx, pub in enumerate(publication_records):
        if idx in used_pubs:
            continue
        unlinked_publications.append(pub)
        studies.append({
            "identity_type": "pmid" if pub.get("pmid") else None,
            "identity": pub.get("pmid"),
            "trial_record": None,
            "publication_records": [pub],
            "link_details": [],
            "record_count": 1,
            "counts_as_studies": 1,
        })

    unlinked_trials = [s["trial_record"] for s in studies
                       if s["trial_record"] is not None and not s["publication_records"]]

    return {
        "studies": studies,
        "independent_study_count": len(studies),
        "total_input_records": len(trial_records) + len(publication_records),
        "linked_pairs": linked_pairs,
        "unlinked_trials": unlinked_trials,
        "unlinked_publications": unlinked_publications,
        "rule": (
            "A registry record and its linked publication describe ONE underlying trial and are "
            "counted once. independent_study_count is the only figure a synthesis may cite."
        ),
    }
