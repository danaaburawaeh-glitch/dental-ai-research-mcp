"""
connectors/crossref/parser.py

Parses Crossref /works responses (JSON) into EvidenceRecord-shaped dicts.
Never invents a missing field.
"""
import json
from errors import CrossrefConnectorError, STATUS_PARSE_ERROR


def parse_work_json(raw_text):
    """Parse a single /works/{doi} response body. Returns dict or raises on malformed JSON."""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CrossrefConnectorError(STATUS_PARSE_ERROR, f"Crossref JSON parse failed: {exc}")

    message = payload.get("message")
    if message is None:
        raise CrossrefConnectorError(STATUS_PARSE_ERROR, "Crossref response missing 'message' field")

    return _message_to_record(message)


def parse_bibliographic_search_json(raw_text):
    """Parse a /works?query.bibliographic=... list response. Returns list of dicts."""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CrossrefConnectorError(STATUS_PARSE_ERROR, f"Crossref JSON parse failed: {exc}")

    message = payload.get("message")
    if message is None:
        raise CrossrefConnectorError(STATUS_PARSE_ERROR, "Crossref response missing 'message' field")

    items = message.get("items", [])
    return [_message_to_record(item) for item in items]


def _message_to_record(message):
    title_list = message.get("title") or []
    title = title_list[0] if title_list else None

    authors = None
    author_list = message.get("author")
    if author_list:
        names = []
        for a in author_list:
            given = a.get("given")
            family = a.get("family")
            if family:
                names.append(f"{given} {family}".strip() if given else family)
        authors = names or None

    container = message.get("container-title") or []
    journal = container[0] if container else None

    doi = message.get("DOI")

    pub_year = None
    pub_date_parts = None
    for date_field in ("published-print", "published-online", "published", "issued"):
        d = message.get(date_field)
        if d and d.get("date-parts") and d["date-parts"][0]:
            pub_date_parts = d["date-parts"][0]
            pub_year = pub_date_parts[0] if pub_date_parts else None
            break

    pub_date_str = "-".join(str(p) for p in pub_date_parts) if pub_date_parts else None

    retraction_info = _parse_retraction_correction(message)

    return {
        "doi": doi,
        "title": title,
        "authors": authors,
        "journal": journal,
        "publication_date": pub_date_str,
        "publication_year": pub_year,
        "publisher": message.get("publisher"),
        "type": message.get("type"),
        "source": "crossref",
        **retraction_info,
    }


# v0.4.2 — verified, directional semantics (CROSSREF_RELATIONSHIP_MAP.md is the canonical
# documentation; this dict is the single source of truth the code actually uses).
#
# Confirmed via real Crossref documentation/blog examples this session:
#   - "update-to" appears on the record that IS the update/notice itself (this record updates
#     the linked DOI).
#   - "updated-by" appears on the record that HAS BEEN updated by another record (the linked
#     DOI is the notice).
# These are opposite directions. v0.4.1 checked only "update-to" and treated it as evidence
# the CURRENT record was retracted — backwards. Fixed here.
#
# The generic "relation" field (is-preprint-of, has-preprint, isVersionOf, etc.) is NOT used
# for retraction/correction signaling in v0.4.2 — no documented evidence was found this session
# that Crossref uses "relation" for this purpose (see CROSSREF_RELATIONSHIP_MAP.md "A finding
# from re-verification"). v0.4.1's substring-matching check against "relation" is removed.
#
# Keyed by (direction, type) -> (sets_is_retracted, sets_is_corrected, record_role)
CROSSREF_UPDATE_SEMANTICS = {
    ("updated-by", "retraction"):            (True,  None,  "article"),
    ("update-to",  "retraction"):             (False, None,  "retraction_notice"),
    ("updated-by", "correction"):             (None,  True,  "article"),
    ("update-to",  "correction"):             (None,  False, "correction_notice"),
    ("updated-by", "erratum"):                (None,  True,  "article"),
    ("update-to",  "erratum"):                (None,  False, "erratum_notice"),
    ("updated-by", "clarification"):          (None,  True,  "article"),
    ("update-to",  "clarification"):          (None,  False, "correction_notice"),
    ("updated-by", "expression_of_concern"):  (None,  None,  "article"),
    ("update-to",  "expression_of_concern"):  (None,  None,  "expression_of_concern_notice"),
}
CROSSREF_CONCERN_TYPE = "expression_of_concern"


def _parse_retraction_correction(message):
    """
    v0.4.2 — Retraction/Correction Safety, directionality-corrected (Sections 1-3 of the
    v0.4.2 patch). Checks 'update-to' (this record IS a notice) and 'updated-by' (this record
    HAS BEEN updated by a notice) as a directional pair, per CROSSREF_UPDATE_SEMANTICS — direct
    lookup only, never substring/name-similarity matching, and never against the generic
    'relation' field (see module-level comment — that field was not confirmed to carry
    retraction signals in Crossref's real, documented behavior).

    Returns dict with publication_status, is_retracted, is_corrected, related_notices,
    retraction_source, record_role — record_role is a separate axis from is_retracted/
    is_corrected (a notice record has a role but is not itself retracted/corrected).
    """
    update_to = message.get("update-to") or []
    updated_by = message.get("updated-by") or []

    notices = []
    is_retracted = False
    is_corrected = False
    record_role = None
    checked_something = bool(update_to) or bool(updated_by)
    has_expression_of_concern = False

    for direction, entries in (("update-to", update_to), ("updated-by", updated_by)):
        for u in entries:
            u_type = (u.get("type") or "").lower()
            semantics = CROSSREF_UPDATE_SEMANTICS.get((direction, u_type))

            if semantics is None:
                # Unknown/unlisted type — preserved verbatim, never classified.
                notices.append({"direction": direction, "type": u_type, "doi": u.get("DOI"),
                                 "label": u.get("label"), "classified": False})
                continue

            sets_retracted, sets_corrected, role = semantics
            notices.append({"direction": direction, "type": u_type, "doi": u.get("DOI"),
                             "label": u.get("label"), "classified": True})
            if sets_retracted is True:
                is_retracted = True
            if sets_corrected is True:
                is_corrected = True
            if record_role is None:
                record_role = role
            if u_type == CROSSREF_CONCERN_TYPE:
                has_expression_of_concern = True

    if is_retracted:
        publication_status = "retracted"
    elif is_corrected:
        publication_status = "corrected"
    elif checked_something:
        publication_status = "active"
    else:
        publication_status = None

    if checked_something and record_role is None:
        record_role = "article"  # checked, no notice-role found — an ordinary article

    return {
        "publication_status": publication_status,
        "is_retracted": is_retracted if checked_something else None,
        "is_corrected": is_corrected if checked_something else None,
        "related_notices": notices or None,
        "retraction_source": "crossref" if (is_retracted or is_corrected or has_expression_of_concern) else None,
        "record_role": record_role if checked_something else None,
    }
