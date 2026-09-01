"""
connectors/shared/citation_verifier.py

v0.4.1 Section 5. Makes the dual-source VERIFIED/PARTIALLY VERIFIED/UNVERIFIED decision
(citation-verification.md's rules) an executable, unit-tested function rather than only a
Markdown instruction that depends on Claude following it correctly each time. This is the
single source of truth for the classification logic — evidence-research's workflow should
call this (via the CLI entry point below, or directly if imported) rather than re-deriving
the comparison independently.
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.normalization import titles_match, years_match, authors_overlap, journals_match
from shared.identifiers import normalize_doi

VERIFIED = "VERIFIED"
PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
UNVERIFIED = "UNVERIFIED"
IDENTIFIER_MISMATCH = "IDENTIFIER_MISMATCH"


def verify_citation(pubmed_record, crossref_record):
    """
    pubmed_record: dict (EvidenceRecord-shaped) or None — the retrieved PubMed record.
    crossref_record: dict (EvidenceRecord-shaped) or None — the retrieved Crossref record.

    Returns:
    {
        "status": VERIFIED | PARTIALLY_VERIFIED | UNVERIFIED | IDENTIFIER_MISMATCH,
        "field_comparisons": {title: bool|None, authors: bool|None, journal: bool|None,
                               year: bool|None, doi: bool|None},
        "mismatch_reasons": [str, ...],
        "basis": str  # human-readable explanation of why this status was reached
    }

    Rules (from citation-verification.md, made executable and unit-tested here):
    - Neither record -> UNVERIFIED (nothing was retrieved at all).
    - PubMed only, no DOI to check -> PARTIALLY_VERIFIED ("no DOI available for cross-check").
    - PubMed only, has DOI, no Crossref record supplied -> PARTIALLY_VERIFIED
      ("Crossref check unavailable/incomplete").
    - Crossref only (no PubMed record) -> PARTIALLY_VERIFIED (Crossref alone never reaches
      VERIFIED — see citation-verification.md; useful but capped).
    - Both records present: compare title/authors/journal/year field-by-field.
        - DOI values present on both and normalized-different -> IDENTIFIER_MISMATCH
          (the strongest, most explicit kind of disagreement).
        - All compared fields match (within documented tolerances) -> VERIFIED.
        - Any compared field disagrees (and isn't just "field missing on one side") -> UNVERIFIED,
          with the specific mismatching field(s) named. Never silently repaired.
        - A field missing on one side is not by itself a mismatch (it just can't be compared
          for that field) — but if EVERY comparable field is missing, that is not enough
          evidence for VERIFIED and is reported as PARTIALLY_VERIFIED instead.
    """
    if pubmed_record is None and crossref_record is None:
        return {
            "status": UNVERIFIED,
            "field_comparisons": {},
            "mismatch_reasons": ["No record retrieved from either source."],
            "basis": "Nothing was retrieved — cannot verify a citation with no source record.",
        }

    if pubmed_record is None:
        return {
            "status": PARTIALLY_VERIFIED,
            "field_comparisons": {},
            "mismatch_reasons": [],
            "basis": "Crossref record only — no PubMed record to cross-check against. "
                     "Crossref-only confirmation is real but capped at PARTIALLY_VERIFIED "
                     "per citation-verification.md.",
        }

    if crossref_record is None:
        pubmed_doi = normalize_doi(pubmed_record.get("doi"))
        if not pubmed_doi:
            return {
                "status": PARTIALLY_VERIFIED,
                "field_comparisons": {},
                "mismatch_reasons": [],
                "basis": "PubMed record confirmed, but it has no DOI available for a "
                         "Crossref cross-check.",
            }
        return {
            "status": PARTIALLY_VERIFIED,
            "field_comparisons": {},
            "mismatch_reasons": [],
            "basis": "PubMed record confirmed and has a DOI, but the Crossref cross-check "
                     "is unavailable or incomplete for this call.",
        }

    # Both records present — DOI identity check first (strongest signal)
    pm_doi = normalize_doi(pubmed_record.get("doi"))
    cr_doi = normalize_doi(crossref_record.get("doi"))
    if pm_doi and cr_doi and pm_doi != cr_doi:
        return {
            "status": IDENTIFIER_MISMATCH,
            "field_comparisons": {"doi": False},
            "mismatch_reasons": [f"DOI mismatch: PubMed={pm_doi!r} vs Crossref={cr_doi!r}"],
            "basis": "The two records disagree on DOI itself — this is a strong-identity "
                     "conflict, not a soft field disagreement. Never silently repaired.",
        }

    comparisons = {}
    reasons = []
    comparable_count = 0

    pm_title, cr_title = pubmed_record.get("title"), crossref_record.get("title")
    if pm_title and cr_title:
        comparable_count += 1
        match = titles_match(pm_title, cr_title)
        comparisons["title"] = match
        if not match:
            reasons.append(f"Title mismatch: PubMed={pm_title!r} vs Crossref={cr_title!r}")
    else:
        comparisons["title"] = None

    pm_authors, cr_authors = pubmed_record.get("authors"), crossref_record.get("authors")
    if pm_authors and cr_authors:
        comparable_count += 1
        match = authors_overlap(pm_authors, cr_authors)
        comparisons["authors"] = match
        if not match:
            reasons.append(f"No author overlap: PubMed={pm_authors!r} vs Crossref={cr_authors!r}")
    else:
        comparisons["authors"] = None

    pm_journal, cr_journal = pubmed_record.get("journal"), crossref_record.get("journal")
    if pm_journal and cr_journal:
        comparable_count += 1
        match = journals_match(pm_journal, cr_journal)
        comparisons["journal"] = match
        if not match:
            reasons.append(f"Journal mismatch: PubMed={pm_journal!r} vs Crossref={cr_journal!r}")
    else:
        comparisons["journal"] = None

    pm_year, cr_year = pubmed_record.get("publication_year"), crossref_record.get("publication_year")
    if pm_year and cr_year:
        comparable_count += 1
        match = years_match(pm_year, cr_year)
        comparisons["year"] = match
        if not match:
            reasons.append(f"Year mismatch (outside +/-1 tolerance): PubMed={pm_year!r} vs Crossref={cr_year!r}")
    else:
        comparisons["year"] = None

    comparisons["doi"] = True if (pm_doi and cr_doi and pm_doi == cr_doi) else (
        None if not (pm_doi and cr_doi) else False
    )

    if comparable_count == 0:
        return {
            "status": PARTIALLY_VERIFIED,
            "field_comparisons": comparisons,
            "mismatch_reasons": [],
            "basis": "Both records were retrieved, but no comparable fields were present on "
                     "both sides to actually cross-check (e.g. missing title/authors/journal/"
                     "year on one or both records) — not enough evidence for VERIFIED.",
        }

    if reasons:
        return {
            "status": UNVERIFIED,
            "field_comparisons": comparisons,
            "mismatch_reasons": reasons,
            "basis": "PubMed and Crossref records disagree on at least one comparable field. "
                     "Never silently repaired — see mismatch_reasons for the specific field(s).",
        }

    return {
        "status": VERIFIED,
        "field_comparisons": comparisons,
        "mismatch_reasons": [],
        "basis": f"PubMed and Crossref records agree on all {comparable_count} comparable field(s).",
    }


def _main():
    parser = argparse.ArgumentParser(description="Dual-source citation verifier")
    parser.add_argument("--pubmed-json", default=None, help="Path to a JSON file with the PubMed EvidenceRecord, or '-' for none")
    parser.add_argument("--crossref-json", default=None, help="Path to a JSON file with the Crossref EvidenceRecord, or '-' for none")
    args = parser.parse_args()

    def load(path):
        if not path or path == "-":
            return None
        with open(path) as f:
            return json.load(f)

    pubmed_record = load(args.pubmed_json)
    crossref_record = load(args.crossref_json)
    result = verify_citation(pubmed_record, crossref_record)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] in (VERIFIED, PARTIALLY_VERIFIED) else 1)


if __name__ == "__main__":
    _main()
