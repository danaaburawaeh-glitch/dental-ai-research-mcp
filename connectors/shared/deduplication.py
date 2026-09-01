"""
connectors/shared/deduplication.py

De-duplicate EvidenceRecords retrieved across multiple connector calls / connectors (Phase 14).
Preferred identity: DOI, then PMID, then normalized-title+year as a cautious fallback.
Ambiguous title-based matches are NEVER auto-merged.

v0.4.1 Section 6 — Deduplication Safety: a shared strong identifier (DOI or PMID) is no longer
sufficient on its own to auto-merge. If two records share a DOI (or PMID) but substantively
disagree on title, or share a PMID but carry conflicting DOIs, that is now FLAGGED_CONFLICT and
the records are kept separate — never silently merged — because a strong-identifier match that
disagrees on substance is more likely a data-quality problem (wrong PMID typo, retracted-and-
republished-with-new-DOI, etc.) than a genuine duplicate.
"""
from .identifiers import identity_key, normalize_doi, normalize_pmid
from .normalization import titles_match, years_match


def deduplicate(records):
    """
    records: list of dicts (EvidenceRecord.to_dict() shape).
    Returns (deduplicated_records, merge_log) where merge_log records every MERGED action,
    every FLAGGED_CONFLICT (strong identifier shared, but metadata substantively disagrees —
    kept separate), and every FLAGGED_AMBIGUOUS_NOT_MERGED (weak title+year match only).
    """
    doi_groups = {}
    pmid_groups = {}   # only for records with a PMID but no DOI
    no_strong_id = []

    for record in records:
        doi = normalize_doi(record.get("doi"))
        pmid = normalize_pmid(record.get("pmid"))
        if doi:
            doi_groups.setdefault(doi, []).append(record)
        elif pmid:
            pmid_groups.setdefault(pmid, []).append(record)
        else:
            no_strong_id.append(record)

    merge_log = []
    result = []

    for doi, group in doi_groups.items():
        merged_group, log_entries = _merge_or_flag_group(group, "doi", doi)
        result.extend(merged_group)
        merge_log.extend(log_entries)

    for pmid, group in pmid_groups.items():
        merged_group, log_entries = _merge_or_flag_group(group, "pmid", pmid)
        result.extend(merged_group)
        merge_log.extend(log_entries)

    # Cross-check: same PMID appearing across records that carry DIFFERENT DOIs (only
    # detectable by looking across the original record list, since same-PMID-different-DOI
    # records land in different doi_groups above and would otherwise never be compared).
    pmid_to_dois = {}
    for record in records:
        pmid = normalize_pmid(record.get("pmid"))
        doi = normalize_doi(record.get("doi"))
        if pmid and doi:
            pmid_to_dois.setdefault(pmid, set()).add(doi)
    for pmid, dois in pmid_to_dois.items():
        if len(dois) > 1:
            merge_log.append({
                "action": "FLAGGED_CONFLICT",
                "identity_basis": "pmid_with_conflicting_doi",
                "pmid": pmid,
                "conflicting_dois": sorted(dois),
                "reason": "Same PMID associated with more than one distinct DOI across "
                          "retrieved records — a strong identifier (PMID) conflicts with "
                          "another strong identifier (DOI). Flagged, not silently merged; "
                          "records for each DOI are kept separate.",
            })

    # Weak fallback: title+year match among records with no strong identifier at all.
    # Never auto-merged, per Phase 14 — only flagged as an ambiguous candidate.
    final_unresolved = []
    consumed = set()
    for i, rec_a in enumerate(no_strong_id):
        if i in consumed:
            continue
        candidate_group = [rec_a]
        for j, rec_b in enumerate(no_strong_id[i + 1:], start=i + 1):
            if j in consumed:
                continue
            if titles_match(rec_a.get("title"), rec_b.get("title")) and \
               years_match(rec_a.get("publication_year"), rec_b.get("publication_year")):
                merge_log.append({
                    "action": "FLAGGED_AMBIGUOUS_NOT_MERGED",
                    "identity_basis": "title_year",
                    "titles": [rec_a.get("title"), rec_b.get("title")],
                    "reason": "Title+year match without DOI/PMID confirmation — "
                              "per Phase 14, ambiguous title-based identity is never auto-merged.",
                })
                candidate_group.append(rec_b)
                consumed.add(j)
        final_unresolved.extend(candidate_group)

    result.extend(final_unresolved)
    return result, merge_log


def _merge_or_flag_group(group, basis, value):
    """
    group: list of >=1 records sharing the same strong identifier (basis/value).
    Sequentially merges records whose titles substantively agree; the moment a title
    disagreement is found against the running merged record, the ENTIRE group is kept
    as separate, unmerged, original records and a FLAGGED_CONFLICT entry is logged —
    per the review requirement, a strong-identifier conflict is surfaced, not silently
    resolved by merging some and not others.
    """
    if len(group) == 1:
        return group, []

    merged = group[0]
    log = []
    conflict = False
    for other in group[1:]:
        m_title, o_title = merged.get("title"), other.get("title")
        if m_title and o_title and not titles_match(m_title, o_title):
            conflict = True
            log.append({
                "action": "FLAGGED_CONFLICT",
                "identity_basis": basis,
                "identity_value": value,
                "reason": f"Same {basis} ({value}) but substantively different titles: "
                          f"{m_title!r} vs {o_title!r}. Flagged, not silently merged.",
            })
        else:
            merged = _merge_prefer_more_complete(merged, other)
            log.append({
                "action": "MERGED",
                "identity_basis": basis,
                "identity_value": value,
                "reason": f"Duplicate {basis} across retrieval paths",
            })

    if conflict:
        return group, log  # keep all originals separate, unmerged
    return [merged], log


def _merge_prefer_more_complete(a, b):
    """Merge two records known (via strong DOI/PMID identity, title-checked) to be the same
    study. Prefers non-null fields; never overwrites a real value with null."""
    merged = dict(a)
    for k, v in b.items():
        if merged.get(k) in (None, "", []) and v not in (None, "", []):
            merged[k] = v
    sources = set()
    for rec in (a, b):
        s = rec.get("source")
        if s:
            sources.add(s)
    if sources:
        merged["source"] = "+".join(sorted(sources))
    return merged
