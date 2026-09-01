"""
connectors/shared/models.py (referenced as the shared EvidenceRecord model, Phase 13)

Common evidence record shape used by both PubMed and Crossref connectors, and by the gateway's
downstream pipeline (DEL-7 tagging, directness, quality appraisal, synthesis).

Design rule: every field defaults to None (unknown), never a guessed or synthesized value.
"Do not invent missing fields. Use null/unknown." (brief, Phase 13, verbatim requirement.)
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, List


@dataclass
class EvidenceRecord:
    id: Optional[str] = None                    # internal id, assigned by identifiers.identity_key
    title: Optional[str] = None
    authors: Optional[List[str]] = None
    journal: Optional[str] = None
    publication_date: Optional[str] = None       # ISO-8601 date string if fully known
    publication_year: Optional[int] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    pmid: Optional[str] = None
    publication_types: Optional[List[str]] = None  # e.g. ["Randomized Controlled Trial"]
    mesh_terms: Optional[List[str]] = None
    source: Optional[str] = None                 # "pubmed" | "crossref"
    retrieved_at: Optional[str] = None            # ISO-8601 timestamp
    query: Optional[str] = None                   # the exact query that retrieved this record
    verification_status: Optional[str] = None      # "VERIFIED" | "PARTIALLY VERIFIED" | "UNVERIFIED"

    # v0.4.1 — Retraction/Correction Safety (Phase 4 of the v0.4.1 patch)
    publication_status: Optional[str] = None       # "active" | "retracted" | "corrected" | None (unknown/unchecked)
    is_retracted: Optional[bool] = None            # True only when structured metadata says so — never inferred from title text
    is_corrected: Optional[bool] = None            # True when a correction/erratum/republication is linked
    related_notices: Optional[List[dict]] = None   # [{type, title, pmid|doi, source}] — retraction/correction/erratum notices, structured
    retraction_source: Optional[str] = None        # "pubmed" | "crossref" — which connector's metadata established the flag

    # v0.4.2 — record_role is a SEPARATE axis from is_retracted/is_corrected/publication_status.
    # A retraction notice is not a retracted article — see PUBMED_CORRECTION_RELATIONSHIP_MAP.md
    # and CROSSREF_RELATIONSHIP_MAP.md. Values: "article" | "retraction_notice" |
    # "correction_notice" | "erratum_notice" | "expression_of_concern_notice" |
    # "corrected_republication" | "unknown" | None (not classified — no structured signal found).
    record_role: Optional[str] = None

    def to_dict(self):
        return {k: v for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, d):
        known_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return cls(**filtered)
