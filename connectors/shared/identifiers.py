"""
connectors/shared/identifiers.py

Shared identifier normalization for deduplication and cross-source matching (Phase 13/14).
No network calls. Pure functions only.
"""
import re
import unicodedata


def normalize_doi(doi):
    """
    Normalize a DOI string for comparison/dedup purposes only.
    Does NOT validate that the DOI resolves — that requires an actual lookup.
    Returns None if input is None or empty.
    """
    if not doi:
        return None
    doi = doi.strip()
    # Strip common URL prefixes if present
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "DOI:"):
        if doi.lower().startswith(prefix.lower()):
            doi = doi[len(prefix):]
            break
    return doi.strip().lower()


def normalize_pmid(pmid):
    """Normalize a PMID to a plain string of digits. Returns None if not a valid-looking PMID."""
    if pmid is None:
        return None
    s = str(pmid).strip()
    if s.isdigit():
        return s
    return None


def normalize_title(title):
    """
    Normalize a title for CAUTIOUS fallback identity comparison only (Phase 14).
    Lowercase, strip punctuation, collapse whitespace, strip diacritics.
    This is NOT a strong identity signal — title-based matching is a last resort,
    and ambiguous title matches must not be auto-merged (see deduplication.py).
    """
    if not title:
        return None
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def identity_key(record):
    """
    Return the best available identity key for a record, in preferred order:
    DOI, then PMID, then (normalized title, year) as a cautious fallback.
    Returns a tuple (key_type, key_value) or (None, None) if no identity is available.
    Per Phase 14: title-based identity is a fallback ONLY, never auto-merged blindly by
    calling code — this function just reports what identity basis is available.
    """
    doi = normalize_doi(record.get("doi"))
    if doi:
        return ("doi", doi)
    pmid = normalize_pmid(record.get("pmid"))
    if pmid:
        return ("pmid", pmid)
    title = normalize_title(record.get("title"))
    year = record.get("publication_year")
    if title and year:
        return ("title_year", (title, year))
    return (None, None)
