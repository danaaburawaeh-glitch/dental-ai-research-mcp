"""
connectors/shared/provenance.py

Machine-readable provenance for every retrieved evidence item (Phase 6).
Kept internally for audit; not necessarily shown verbatim to ordinary clinical users
(SKILL.md-level presentation logic decides what to surface — this module only builds the record).
"""
import datetime
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Provenance:
    source_connector: str          # "pubmed" | "crossref"
    source_database: str           # "pubmed" | "crossref-works"
    retrieval_timestamp: str       # ISO-8601, UTC
    query: str
    pmid: Optional[str] = None
    doi: Optional[str] = None
    retrieval_status: str = "UNKNOWN"          # see errors.py status taxonomy
    citation_verification_status: Optional[str] = None  # VERIFIED / PARTIALLY VERIFIED / UNVERIFIED

    def to_dict(self):
        return asdict(self)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def build_provenance(source_connector, source_database, query, retrieval_status,
                      pmid=None, doi=None, citation_verification_status=None):
    return Provenance(
        source_connector=source_connector,
        source_database=source_database,
        retrieval_timestamp=now_iso(),
        query=query,
        pmid=pmid,
        doi=doi,
        retrieval_status=retrieval_status,
        citation_verification_status=citation_verification_status,
    )
