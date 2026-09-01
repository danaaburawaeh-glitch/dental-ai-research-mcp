"""
connectors/crossref/models.py

Crossref-specific field mapping constants.
"""

# Crossref 'type' values relevant to evidence work (not exhaustive — Crossref supports many).
TYPE_JOURNAL_ARTICLE = "journal-article"
TYPE_BOOK_CHAPTER = "book-chapter"
TYPE_PROCEEDINGS_ARTICLE = "proceedings-article"
TYPE_POSTED_CONTENT = "posted-content"  # preprints

CAPABILITY_LABEL = "CONNECTED — METADATA/CITATION VERIFICATION"
CAPABILITY_LABEL_NOT_FULL_TEXT_NOTE = (
    "Crossref is not a full-text journal-access system. It provides bibliographic metadata "
    "(title, authors, container-title, date, publisher, type, and often a link to the "
    "publisher's own full-text location) and DOI resolution. Never label Crossref connectivity "
    "as 'CONNECTED — FULL TEXT' — see connector-capability-map.md and Phase 11 of the v0.4 brief."
)
