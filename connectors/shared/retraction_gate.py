"""
connectors/shared/retraction_gate.py

v0.4.2 Section 4/5. Makes retraction-correction-gate.md's rules an executable, unit-tested
function rather than only a Markdown instruction — same reasoning as v0.4.1's citation_verifier.py
for the dual-source verification decision.

Input: list of EvidenceRecord-shaped dicts (already retraction/correction-parsed — i.e. with
is_retracted, is_corrected, record_role, related_notices already populated by the connector
parsers).

Output: {"included": [...], "excluded": [...], "flagged": [...]}
- included: records safe to route into DIRECT/INDIRECT evidence synthesis buckets.
- excluded: records removed from supporting-evidence consideration entirely (retracted articles),
  each carrying an "exclusion_reason" field.
- flagged: records that ARE allowed through but carry a caution annotation the synthesis step
  must surface (retraction notices treated as contextual, not clinical, evidence; unresolved
  corrections; expressions of concern) — each carrying a "flag_reason" field.

A record can appear in exactly one of the three lists — never more than one, never zero (every
input record is accounted for somewhere in the output).
"""
import argparse
import json
import sys

REASON_RETRACTED = "RETRACTED — EXCLUDED FROM SYNTHESIS"
REASON_RETRACTION_NOTICE = "RETRACTION NOTICE — CONTEXTUAL ONLY, NOT CLINICAL SUPPORTING EVIDENCE"
REASON_CORRECTION_UNRESOLVED = "CORRECTION EXISTS — VERIFY CURRENT VERSION"
REASON_EXPRESSION_OF_CONCERN = "EXPRESSION OF CONCERN — USE WITH HEIGHTENED CAUTION"

NOTICE_ROLES = {"retraction_notice", "correction_notice", "erratum_notice",
                "expression_of_concern_notice", "corrected_republication"}


def apply_retraction_gate(records, resolve_corrected_version=None):
    """
    records: list of EvidenceRecord-shaped dicts.
    resolve_corrected_version: optional callable(record) -> resolved-record-or-None, used to
    look up a corrected version by the PMID/DOI in related_notices. If not supplied (the
    default), a correction is always treated as unresolved (conservative default — never
    silently assumes a correction is resolved without being told how to resolve it).

    Returns {"included": [...], "excluded": [...], "flagged": [...]}.
    """
    included, excluded, flagged = [], [], []

    for record in records:
        is_retracted = record.get("is_retracted")
        is_corrected = record.get("is_corrected")
        record_role = record.get("record_role")
        related_notices = record.get("related_notices") or []

        # Rule: retracted article -> excluded, regardless of any other status.
        if is_retracted is True:
            excluded.append({**record, "exclusion_reason": REASON_RETRACTED})
            continue

        # Rule: this record IS a notice of some kind -> not clinical supporting evidence,
        # routed to flagged/contextual, never into the direct evidence pool.
        if record_role == "retraction_notice":
            flagged.append({**record, "flag_reason": REASON_RETRACTION_NOTICE})
            continue
        if record_role in NOTICE_ROLES:
            # Other notice roles (correction/erratum/expression-of-concern notices,
            # corrected-republication records) are also not themselves the clinical article —
            # flagged as contextual with a role-specific reason, never silently included as if
            # they were ordinary supporting evidence.
            flagged.append({**record, "flag_reason": f"{record_role.upper()} — CONTEXTUAL RECORD, NOT THE SOURCE ARTICLE"})
            continue

        # Rule: corrected article -> prefer resolving the corrected version if a resolver was
        # given; otherwise flag as unresolved. Never silently substitutes one for the other.
        if is_corrected is True:
            resolved = None
            if resolve_corrected_version is not None:
                resolved = resolve_corrected_version(record)
            if resolved is not None:
                merged = dict(resolved)
                merged["superseded_record"] = {
                    "pmid": record.get("pmid"), "doi": record.get("doi"),
                    "title": record.get("title"),
                }
                included.append(merged)
            else:
                flagged.append({**record, "flag_reason": REASON_CORRECTION_UNRESOLVED})
            continue

        # Rule: expression of concern present in related_notices (but not itself retracted or
        # corrected) -> included, but flagged for heightened caution, never treated as strong
        # supporting evidence without explicit review.
        has_concern = any(
            n.get("classified") and (
                n.get("type") in ("ExpressionOfConcernIn",) or
                n.get("type") == "expression_of_concern"
            )
            for n in related_notices
        )
        if has_concern:
            flagged.append({**record, "flag_reason": REASON_EXPRESSION_OF_CONCERN})
            continue

        # Clean: not retracted, not a notice, not corrected, no concern -> included normally.
        included.append(record)

    return {"included": included, "excluded": excluded, "flagged": flagged}


def _main():
    parser = argparse.ArgumentParser(description="Apply the retraction/correction evidence gate")
    parser.add_argument("--records-json", required=True, help="Path to a JSON file containing a list of EvidenceRecord dicts")
    args = parser.parse_args()

    with open(args.records_json) as f:
        records = json.load(f)

    result = apply_retraction_gate(records)
    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    _main()
