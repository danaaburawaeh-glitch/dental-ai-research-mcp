"""
connectors/pubmed/parser.py

Parses ESearch/ESummary/EFetch XML into EvidenceRecord-shaped dicts.
Never invents a missing field — absent XML nodes become None, per Phase 13.
"""
import xml.etree.ElementTree as ET
from errors import PubMedConnectorError, STATUS_PARSE_ERROR
from models import PUBTYPE_SYSTEMATIC_REVIEW, PUBTYPE_META_ANALYSIS


def parse_esearch_xml(xml_text):
    """Returns (pmids: list[str], count: int, query_translation: str|None)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PubMedConnectorError(STATUS_PARSE_ERROR, f"ESearch XML parse failed: {exc}")

    count_el = root.find("Count")
    count = int(count_el.text) if count_el is not None and count_el.text else 0
    pmids = [id_el.text for id_el in root.findall("./IdList/Id") if id_el.text]
    translation_el = root.find("QueryTranslation")
    query_translation = translation_el.text if translation_el is not None else None
    return pmids, count, query_translation


def parse_efetch_pubmed_xml(xml_text):
    """
    Parses EFetch retmode=xml PubmedArticle records into a list of EvidenceRecord-shaped dicts.
    Fields not present in the XML are left as None — never guessed.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PubMedConnectorError(STATUS_PARSE_ERROR, f"EFetch XML parse failed: {exc}")

    records = []
    for article_el in root.findall(".//PubmedArticle"):
        records.append(_parse_single_article(article_el))
    return records


def _text_or_none(el):
    return el.text if el is not None and el.text else None


def _parse_single_article(article_el):
    medline = article_el.find("MedlineCitation")
    article = medline.find("Article") if medline is not None else None

    pmid = _text_or_none(medline.find("PMID")) if medline is not None else None
    title = _text_or_none(article.find("ArticleTitle")) if article is not None else None

    # Abstract may have multiple AbstractText nodes (structured abstracts) — join if so.
    abstract = None
    if article is not None:
        abstract_els = article.findall(".//AbstractText")
        if abstract_els:
            parts = []
            for el in abstract_els:
                label = el.get("Label")
                text = el.text or ""
                parts.append(f"{label}: {text}" if label else text)
            abstract = "\n".join(p for p in parts if p.strip()) or None

    # Journal
    journal = None
    if article is not None:
        journal_el = article.find("Journal/Title")
        if journal_el is None:
            journal_el = article.find("Journal/ISOAbbreviation")
        journal = _text_or_none(journal_el)

    # Publication date/year
    pub_year = None
    pub_date_str = None
    if article is not None:
        pubdate_el = article.find("Journal/JournalIssue/PubDate")
        if pubdate_el is not None:
            year_el = pubdate_el.find("Year")
            if year_el is not None and year_el.text and year_el.text.isdigit():
                pub_year = int(year_el.text)
            medline_date_el = pubdate_el.find("MedlineDate")
            if pub_year is None and medline_date_el is not None and medline_date_el.text:
                # e.g. "2019 Jan-Feb" — extract a leading 4-digit year cautiously
                candidate = medline_date_el.text.strip()[:4]
                if candidate.isdigit():
                    pub_year = int(candidate)
            month_el = pubdate_el.find("Month")
            day_el = pubdate_el.find("Day")
            if pub_year is not None:
                month = month_el.text if month_el is not None else None
                day = day_el.text if day_el is not None else None
                pub_date_str = "-".join(filter(None, [str(pub_year), month, day]))

    # Authors
    authors = None
    if article is not None:
        author_els = article.findall(".//AuthorList/Author")
        if author_els:
            names = []
            for a in author_els:
                last = _text_or_none(a.find("LastName"))
                fore = _text_or_none(a.find("ForeName"))
                collective = _text_or_none(a.find("CollectiveName"))
                if collective:
                    names.append(collective)
                elif last:
                    names.append(f"{fore} {last}".strip() if fore else last)
            authors = names or None

    # Publication types (structured — this is what governs design classification, never
    # free-text title matching, per the RCT disambiguation requirement)
    pub_types = None
    if article is not None:
        pt_els = article.findall(".//PublicationTypeList/PublicationType")
        if pt_els:
            pub_types = [t.text for t in pt_els if t.text]

    # MeSH terms
    mesh_terms = None
    if medline is not None:
        mesh_els = medline.findall(".//MeshHeadingList/MeshHeading/DescriptorName")
        if mesh_els:
            mesh_terms = [m.text for m in mesh_els if m.text]

    # DOI — found in ArticleIdList (PubmedData) or ELocationID
    doi = None
    pubmed_data = article_el.find("PubmedData")
    if pubmed_data is not None:
        for id_el in pubmed_data.findall(".//ArticleIdList/ArticleId"):
            if id_el.get("IdType") == "doi" and id_el.text:
                doi = id_el.text
                break
    if doi is None and article is not None:
        for eloc in article.findall("ELocationID"):
            if eloc.get("EIdType") == "doi" and eloc.text:
                doi = eloc.text
                break

    retraction_info = _parse_retraction_correction(medline, pub_types)

    return {
        "pmid": pmid,
        "title": title,
        "authors": authors,
        "journal": journal,
        "publication_date": pub_date_str,
        "publication_year": pub_year,
        "abstract": abstract,
        "doi": doi,
        "publication_types": pub_types,
        "mesh_terms": mesh_terms,
        "source": "pubmed",
        **retraction_info,
    }


# v0.4.2 — explicit, directional RefType semantics (PUBMED_CORRECTION_RELATIONSHIP_MAP.md is the
# canonical documentation; this dict is the single source of truth the code actually uses — a
# direct lookup, never substring/name-similarity matching). Each entry:
# (sets_is_retracted, sets_is_corrected, record_role_if_this_type_seen)
# None for sets_is_retracted/sets_is_corrected means "does not affect that flag".
PUBMED_REFTYPE_SEMANTICS = {
    "RetractionIn":               (True,  None,  "article"),
    "RetractionOf":                (False, None,  "retraction_notice"),
    "ErratumIn":                   (None,  True,  "article"),
    "ErratumFor":                  (None,  False, "erratum_notice"),
    "CorrectedAndRepublishedIn":   (None,  True,  "article"),
    "CorrectedAndRepublishedFrom": (None,  False, "corrected_republication"),
    "ExpressionOfConcernIn":       (None,  None,  "article"),
    "ExpressionOfConcernFor":      (None,  None,  "expression_of_concern_notice"),
}
EXPRESSION_OF_CONCERN_TYPES = ("ExpressionOfConcernIn", "ExpressionOfConcernFor")


def _parse_retraction_correction(medline, pub_types):
    """
    v0.4.2 — Retraction/Correction Safety, directionality-corrected (Section 1/2/3 of the
    v0.4.2 patch). Structured PubMed signals only, never free-text title matching:

    1. PublicationTypeList itself may directly contain "Retracted Publication" (this record
       IS a retracted article) or "Retraction of Publication" (this record IS the retraction
       notice for another article) — these are DIFFERENT roles, not the same fact from two
       angles.
    2. CommentsCorrectionsList/CommentsCorrections RefType — looked up directly in
       PUBMED_REFTYPE_SEMANTICS. v0.4.1 incorrectly collapsed *In/*Of and *In/*For pairs into
       the same meaning (e.g. treating RetractionOf as proof the CURRENT record is retracted,
       when it actually means the current record IS the retraction notice for something else).
       Fixed here — each RefType is looked up for its own, specific, directional meaning.

    Returns dict with publication_status, is_retracted, is_corrected, related_notices,
    retraction_source, record_role — all None/empty unless structured evidence was found, and
    record_role is tracked as a SEPARATE axis from is_retracted/is_corrected (a retraction
    notice has record_role="retraction_notice" and is_retracted=False — it is not itself a
    retracted article).
    """
    pub_types = pub_types or []
    is_retracted = False
    is_corrected = False
    record_role = None
    notices = []
    checked_something = bool(pub_types)

    if "Retracted Publication" in pub_types:
        is_retracted = True
        record_role = "article"
    elif "Retraction of Publication" in pub_types:
        record_role = "retraction_notice"
        # is_retracted stays False — a retraction NOTICE is not itself a retracted article.

    if medline is not None:
        for cc in medline.findall(".//CommentsCorrectionsList/CommentsCorrections"):
            ref_type = cc.get("RefType")
            note_pmid = _text_or_none(cc.find("PMID"))
            note_source = _text_or_none(cc.find("RefSource"))
            semantics = PUBMED_REFTYPE_SEMANTICS.get(ref_type)

            if semantics is None:
                # Unknown/unlisted RefType — preserved verbatim, never classified.
                # (Phase 3 instruction: "preserve them ... but do NOT convert them ... without
                # a known directional mapping.")
                notices.append({"type": ref_type, "pmid": note_pmid, "source_text": note_source,
                                 "classified": False})
                continue

            checked_something = True
            sets_retracted, sets_corrected, role = semantics
            notices.append({"type": ref_type, "pmid": note_pmid, "source_text": note_source,
                             "classified": True})

            if sets_retracted is True:
                is_retracted = True
            if sets_corrected is True:
                is_corrected = True
            if record_role is None:
                record_role = role

    has_expression_of_concern = any(
        n["type"] in EXPRESSION_OF_CONCERN_TYPES for n in notices if n.get("classified")
    )

    if is_retracted:
        publication_status = "retracted"
    elif is_corrected:
        publication_status = "corrected"
    elif checked_something:
        publication_status = "active"
    else:
        publication_status = None

    if checked_something and record_role is None:
        # Checked, and no notice-role (retraction/erratum/concern notice) was found — this
        # is an ordinary article, a positive finding, not "unclassified." Consistent with
        # is_retracted/is_corrected also defaulting to False (not None) once actually checked.
        record_role = "article"

    return {
        "publication_status": publication_status,
        "is_retracted": is_retracted if checked_something else None,
        "is_corrected": is_corrected if checked_something else None,
        "related_notices": notices or None,
        "retraction_source": "pubmed" if (is_retracted or is_corrected or has_expression_of_concern) else None,
        "record_role": record_role if checked_something else None,
    }


def is_actually_systematic_review(publication_types):
    """
    Hard rule (Phase 3): classify as systematic review/meta-analysis ONLY if PubMed's own
    PublicationType metadata says so. Never infer from title text containing the phrase.
    """
    if not publication_types:
        return False
    return any(pt in (PUBTYPE_SYSTEMATIC_REVIEW, PUBTYPE_META_ANALYSIS) for pt in publication_types)
