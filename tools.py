"""
tools.py

The four public MCP tools, and the health check.

Scope is deliberately fixed at four tools:
    search_pubmed, search_systematic_reviews, verify_citation, search_clinical_trials

Design rules enforced here
--------------------------
1. Retrieval logic is NOT reimplemented. Every network call goes through the vendored,
   validated Dental AI v1.0.2 connector code via connector_bridge. This module only
   normalizes that output into the agreed tool contracts.

2. Identifiers are never fabricated. A PMID, DOI or NCT ID is emitted only when the
   upstream payload actually carried it. There is no default, no placeholder, no
   reconstruction from a title.

3. A retrieval failure is a failure, never "no evidence". UPSTREAM_ERROR / TIMEOUT /
   PARSE_ERROR / RATE_LIMITED are surfaced verbatim as `status` with `ok: false`.
   ZERO_RESULTS is a distinct, successful outcome meaning "this query matched nothing",
   which is NOT the same claim as "no such evidence exists".

4. Every response carries provenance: which connector, which database, the exact query
   sent upstream, and a UTC retrieval timestamp.
"""
import datetime

import connector_bridge

# Upstream statuses that mean "the query ran and the answer is trustworthy".
_OK_STATUSES = ("SUCCESS", "ZERO_RESULTS")

MAX_RESULTS_CAP = 50
DEFAULT_MAX_RESULTS = 10

REGISTRY_CAVEAT = (
    "A ClinicalTrials.gov registry record documents that a study was registered. "
    "It is NOT evidence that an intervention works, and a registered trial may be "
    "unpublished, incomplete, terminated, or contradicted by later evidence."
)

SR_CAVEAT = (
    "Filtered on PubMed's structured Publication Type field "
    "(\"Systematic Review\"[pt] OR \"Meta-Analysis\"[pt]) only, never on title text. "
    "This is PubMed coverage. It is NOT Cochrane/CENTRAL access and must not be "
    "described as a Cochrane search — Cochrane, Embase and Scopus are NOT IMPLEMENTED."
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _clamp(max_results):
    try:
        n = int(max_results) if max_results is not None else DEFAULT_MAX_RESULTS
    except (TypeError, ValueError):
        n = DEFAULT_MAX_RESULTS
    return max(1, min(n, MAX_RESULTS_CAP))


def _upstream_error(status, message, source, database, query):
    """
    Explicit upstream failure. Never collapses into an empty result set, because an
    empty result set would read as 'no evidence found' — a clinical claim we did not earn.
    """
    return {
        "ok": False,
        "status": status or "UPSTREAM_ERROR",
        "error": message or "Upstream retrieval failed.",
        "interpretation": (
            "RETRIEVAL FAILED — this is an upstream/network failure, NOT a finding of "
            "'no evidence'. No claim about the literature may be drawn from this response."
        ),
        "results": None,
        "source": source,
        "source_provenance": {
            "source_connector": source,
            "source_database": database,
            "query": query,
            "retrieval_status": status or "UPSTREAM_ERROR",
            "retrieved_at": _now(),
        },
        "retrieved_at": _now(),
    }


# --------------------------------------------------------------------------
# Publication normalization (shared by search_pubmed / search_systematic_reviews)
# --------------------------------------------------------------------------

def _normalize_publication(rec, query):
    """
    Maps a vendored PubMed EvidenceRecord dict onto the agreed publication contract.
    Absent fields stay null. Nothing is inferred, guessed or back-filled.
    """
    pmid = rec.get("pmid")
    doi = rec.get("doi")
    return {
        "pmid": pmid,
        "title": rec.get("title"),
        "authors": rec.get("authors"),
        "journal": rec.get("journal"),
        "publication_date": rec.get("publication_date"),
        "publication_year": rec.get("publication_year"),
        "publication_type": rec.get("publication_types"),
        "doi": doi,
        "abstract": rec.get("abstract"),
        "source": "pubmed",
        "retrieved_at": rec.get("retrieved_at") or _now(),
        "url": ("https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid) if pmid else None,
        "source_provenance": {
            "source_connector": "pubmed",
            "source_database": "pubmed",
            "query": query,
            "pmid": pmid,
            "doi": doi,
            "retrieval_status": "SUCCESS",
            "retrieved_at": rec.get("retrieved_at") or _now(),
        },
    }


def _pubmed_search_then_fetch(query, max_results, date_from, date_to, systematic_only):
    """
    ESearch to get PMIDs, then EFetch to hydrate them. Both hops are the validated
    v1.0.2 code path. Either hop failing produces an explicit upstream error.
    """
    pm = connector_bridge.get("pubmed")
    n = _clamp(max_results)

    date_range = None
    if date_from or date_to:
        # PubMed wants both ends; open-ended ranges are bounded with permissive defaults.
        date_range = (date_from or "1800", date_to or "3000")

    if systematic_only:
        search = pm.pubmed_search_systematic_reviews(query, max_results=n)
    else:
        search = pm.pubmed_search(query, date_range=date_range, max_results=n)

    status = search.get("status")
    if status not in _OK_STATUSES:
        return _upstream_error(status, search.get("message") or search.get("error"),
                               "pubmed", "pubmed", query)

    pmids = search.get("pmids") or []
    executed = search.get("raw_query") or query

    if not pmids:
        return {
            "ok": True,
            "status": "ZERO_RESULTS",
            "interpretation": (
                "The query executed successfully and matched no PubMed records. "
                "This means this query found nothing — it does NOT establish that no "
                "relevant evidence exists."
            ),
            "total_matched": search.get("count", 0),
            "returned": 0,
            "results": [],
            "executed_query": executed,
            "source": "pubmed",
            "retrieved_at": _now(),
        }

    fetched = pm.pubmed_fetch(pmids)
    fstatus = fetched.get("status")
    if fstatus not in _OK_STATUSES:
        return _upstream_error(fstatus, fetched.get("message") or fetched.get("error"),
                               "pubmed", "pubmed", query)

    results = [_normalize_publication(r, executed) for r in (fetched.get("records") or [])]

    # Never emit a record without the identifier that makes it checkable.
    results = [r for r in results if r.get("pmid")]

    return {
        "ok": True,
        "status": "SUCCESS",
        "total_matched": search.get("count"),
        "returned": len(results),
        "results": results,
        "executed_query": executed,
        "query_translation": search.get("query_translation"),
        "source": "pubmed",
        "retrieved_at": _now(),
    }


# --------------------------------------------------------------------------
# Tool 1 — search_pubmed
# --------------------------------------------------------------------------

def search_pubmed(query, max_results=None, date_from=None, date_to=None):
    if not query or not str(query).strip():
        return _upstream_error("INVALID_INPUT", "query is required and must be non-empty.",
                               "pubmed", "pubmed", query)
    return _pubmed_search_then_fetch(query, max_results, date_from, date_to,
                                     systematic_only=False)


# --------------------------------------------------------------------------
# Tool 2 — search_systematic_reviews
# --------------------------------------------------------------------------

def search_systematic_reviews(query, max_results=None):
    if not query or not str(query).strip():
        return _upstream_error("INVALID_INPUT", "query is required and must be non-empty.",
                               "pubmed", "pubmed", query)
    out = _pubmed_search_then_fetch(query, max_results, None, None, systematic_only=True)
    out["filter_applied"] = '("Systematic Review"[Publication Type] OR "Meta-Analysis"[Publication Type])'
    out["coverage_caveat"] = SR_CAVEAT
    out["cochrane_status"] = "NOT IMPLEMENTED"
    out["embase_status"] = "NOT IMPLEMENTED"
    out["scopus_status"] = "NOT IMPLEMENTED"
    return out


# --------------------------------------------------------------------------
# Tool 3 — verify_citation
# --------------------------------------------------------------------------

def _crossref_record(doi):
    cr = connector_bridge.get("crossref")
    res = cr.crossref_lookup_doi(doi)
    return res


def _pubmed_record_for(pmid=None, doi=None, title=None):
    """Best available PubMed metadata, by PMID, else by DOI, else by exact-ish title."""
    pm = connector_bridge.get("pubmed")
    if pmid:
        f = pm.pubmed_fetch([str(pmid)])
        if f.get("status") == "SUCCESS" and f.get("records"):
            return f["records"][0], None
        if f.get("status") not in _OK_STATUSES:
            return None, f
        return None, None

    term = None
    if doi:
        term = "%s[AID]" % doi
    elif title:
        term = "%s[Title]" % title
    if not term:
        return None, None

    s = pm.pubmed_search(term, max_results=1)
    if s.get("status") not in _OK_STATUSES:
        return None, s
    pmids = s.get("pmids") or []
    if not pmids:
        return None, None
    f = pm.pubmed_fetch(pmids)
    if f.get("status") == "SUCCESS" and f.get("records"):
        return f["records"][0], None
    if f.get("status") not in _OK_STATUSES:
        return None, f
    return None, None


def _norm_title(t):
    if not t:
        return ""
    return "".join(ch.lower() for ch in t if ch.isalnum() or ch.isspace()).split()


# --------------------------------------------------------------------------
# Citation verification parity with the plugin's v1.2 evidence layer.
#
# These constants and comparators mirror
# plugin/dana-dental-research/evidence/citation_verification.py and
# connectors/shared/normalization.py. They exist here so that BOTH transports return the same
# citation semantics for the same pair of records — a client must never have to know which
# transport answered in order to interpret the verdict.
#
# The specific divergence this closes: this server previously compared only title, year and DOI,
# with the year by exact equality. A record whose Crossref issue year is one calendar year after
# its PubMed online-first year came back NOT_VERIFIED, while the plugin's local path returned a
# confirmed citation for the same record. Real example encountered in validation:
# DOI 10.5005/jp-journals-10024-3981 (PubMed 2025, Crossref 2026).
# --------------------------------------------------------------------------

ONLINE_FIRST_YEAR_TOLERANCE = 1
DISCREPANCY_ONLINE_FIRST = "ONLINE_FIRST_VS_ISSUE_YEAR"

STATUS_VERIFIED = "VERIFIED"
STATUS_VERIFIED_WITH_METADATA_DISCREPANCY = "VERIFIED_WITH_METADATA_DISCREPANCY"
STATUS_PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
STATUS_NOT_VERIFIED = "NOT_VERIFIED"

_JOURNAL_STOPWORDS = {"journal", "of", "the", "international", "and", "for"}


def _surname(name):
    """PubMed renders "Smith J" (surname first, initials last); Crossref renders "John Smith".
    Trailing initials tokens are dropped and the last of what remains is the surname, so both
    renderings — and compound surnames in either order — reduce to the same key."""
    parts = str(name or "").replace(",", " ").split()
    while len(parts) > 1 and len(parts[-1]) <= 3 and parts[-1].isalpha() and parts[-1].isupper():
        parts.pop()
    if not parts:
        return None
    return parts[-1].strip().lower()


def _authors_match(a, b):
    """Substantial author agreement = at least one shared surname. Returns None when either
    side has no author list, because that is an absence, not a disagreement."""
    if not a or not b:
        return None
    sa = {s for s in (_surname(x) for x in a) if s}
    sb = {s for s in (_surname(x) for x in b) if s}
    if not sa or not sb:
        return None
    return len(sa & sb) >= 1


def _journals_match(a, b):
    """Tolerant of abbreviation: PubMed returns ISO abbreviations ("Clin Oral Investig"),
    Crossref returns full titles ("Clinical Oral Investigations")."""
    if not a or not b:
        return None
    # _norm_title already returns a token LIST, not a string.
    ta = [t for t in (_norm_title(a) or []) if t]
    tb = [t for t in (_norm_title(b) or []) if t]
    if not ta or not tb:
        return None
    if ta == tb:
        return True
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    j = 0
    ok = True
    for tok in shorter:
        found = False
        while j < len(longer):
            cand = longer[j]
            if cand.startswith(tok) or tok.startswith(cand):
                found = True
                j += 1
                break
            if cand not in _JOURNAL_STOPWORDS:
                ok = False
                break
            j += 1
        if not ok or not found:
            ok = False
            break
    if ok:
        return True
    ca = set(ta) - _JOURNAL_STOPWORDS
    cb = set(tb) - _JOURNAL_STOPWORDS
    if not ca or not cb:
        return None
    return (len(ca & cb) / float(max(len(ca), len(cb)))) >= 0.6


def _compare_years(pm_year, cr_year):
    """Returns (verdict, gap) where verdict is True (identical), "WITHIN_TOLERANCE",
    False (beyond tolerance), or None (not comparable)."""
    if not pm_year or not cr_year:
        return None, None
    try:
        gap = abs(int(pm_year) - int(cr_year))
    except (TypeError, ValueError):
        return None, None
    if gap == 0:
        return True, 0
    if gap <= ONLINE_FIRST_YEAR_TOLERANCE:
        return "WITHIN_TOLERANCE", gap
    return False, gap


def _titles_match(a, b):
    ta, tb = _norm_title(a), _norm_title(b)
    if not ta or not tb:
        return None
    if ta == tb:
        return True
    sa, sb = set(ta), set(tb)
    overlap = len(sa & sb) / float(max(len(sa | sb), 1))
    return overlap >= 0.8


def verify_citation(doi=None, pmid=None, title=None):
    """
    Verifies a citation against Crossref and PubMed.

    Never invents a DOI or PMID: an identifier appears in the output only if an upstream
    record actually carried it. A DOI that resolves to nothing returns NOT_VERIFIED —
    which is a real, useful finding, distinct from an upstream failure.
    """
    if not any([doi, pmid, title]):
        return _upstream_error("INVALID_INPUT",
                               "At least one of doi, pmid or title is required.",
                               "crossref+pubmed", "crossref-works+pubmed", None)

    query_desc = {"doi": doi, "pmid": pmid, "title": title}
    sources = []
    cr_rec = None
    cr_status = None

    if doi:
        cr = _crossref_record(doi)
        cr_status = cr.get("status")
        if cr_status in ("UPSTREAM_ERROR", "TIMEOUT", "PARSE_ERROR", "RATE_LIMITED"):
            return _upstream_error(cr_status, cr.get("message") or cr.get("error"),
                                   "crossref", "crossref-works", doi)
        if cr_status == "SUCCESS":
            cr_rec = cr.get("record")
            sources.append("crossref")

    pm_rec, pm_fail = _pubmed_record_for(pmid=pmid, doi=doi, title=title)
    if pm_fail is not None and pm_fail.get("status") in ("UPSTREAM_ERROR", "TIMEOUT",
                                                          "PARSE_ERROR", "RATE_LIMITED"):
        return _upstream_error(pm_fail.get("status"),
                               pm_fail.get("message") or pm_fail.get("error"),
                               "pubmed", "pubmed", doi or pmid or title)
    if pm_rec:
        sources.append("pubmed")

    # ---- decide verification status (identifiers only ever copied, never synthesized) ----
    out_doi = None
    out_pmid = None
    out_title = None
    out_journal = None
    out_year = None
    out_authors = None

    if cr_rec:
        out_doi = cr_rec.get("doi")
        out_title = cr_rec.get("title")
        out_journal = cr_rec.get("journal") or cr_rec.get("container_title")
        out_year = cr_rec.get("publication_year") or cr_rec.get("year")
        out_authors = cr_rec.get("authors")
    if pm_rec:
        out_pmid = pm_rec.get("pmid")
        out_doi = out_doi or pm_rec.get("doi")
        out_title = out_title or pm_rec.get("title")
        out_journal = out_journal or pm_rec.get("journal")
        out_year = out_year or pm_rec.get("publication_year")
        out_authors = out_authors or pm_rec.get("authors")

    metadata_match = {}
    year_detail = {}
    if cr_rec and pm_rec:
        cy = cr_rec.get("publication_year") or cr_rec.get("year")
        py = pm_rec.get("publication_year")
        year_verdict, year_gap = _compare_years(py, cy)

        metadata_match["title"] = _titles_match(cr_rec.get("title"), pm_rec.get("title"))
        metadata_match["authors"] = _authors_match(cr_rec.get("authors"), pm_rec.get("authors"))
        metadata_match["journal"] = _journals_match(
            cr_rec.get("journal") or cr_rec.get("container_title"), pm_rec.get("journal"))
        # `year` stays boolean-or-None for backward compatibility: a within-tolerance difference
        # is not an agreement, so it reports False here, and the full three-valued reading is in
        # `year_comparison` / `discrepancy_type` alongside it.
        metadata_match["year"] = (True if year_verdict is True
                                  else None if year_verdict is None else False)
        cd = (cr_rec.get("doi") or "").lower().strip()
        pd = (pm_rec.get("doi") or "").lower().strip()
        metadata_match["doi"] = (cd == pd) if (cd and pd) else None

        year_detail = {
            "pubmed_year": py,
            "crossref_year": cy,
            "year_gap": year_gap,
            "year_tolerance": ONLINE_FIRST_YEAR_TOLERANCE,
            "year_comparison": ("MATCH" if year_verdict is True
                                else "WITHIN_TOLERANCE" if year_verdict == "WITHIN_TOLERANCE"
                                else "MISMATCH" if year_verdict is False
                                else "NOT_COMPARABLE"),
            "year_source_names": {"pubmed_year_from": "pubmed",
                                  "crossref_year_from": "crossref"},
        }

        identity_established = (metadata_match["doi"] is True) or all(
            metadata_match.get(f) is True for f in ("title", "authors", "journal"))
        non_year_mismatch = [f for f in ("title", "authors", "journal", "doi")
                             if metadata_match.get(f) is False]
        comparable = [v for f, v in metadata_match.items()
                      if f != "year" and v is not None]

        if non_year_mismatch:
            status = STATUS_NOT_VERIFIED
            note = ("Crossref and PubMed both returned a record but they disagree on "
                    + ", ".join(non_year_mismatch) +
                    ". Treat this citation as unconfirmed until resolved. Neither value has "
                    "been altered or preferred.")
        elif year_verdict == "WITHIN_TOLERANCE" and identity_established:
            status = STATUS_VERIFIED_WITH_METADATA_DISCREPANCY
            note = ("Identity is confirmed (DOI, title, authors and journal agree) and only the "
                    f"publication year differs, by {year_gap} year, within the documented "
                    f"online-first versus print/issue tolerance of {ONLINE_FIRST_YEAR_TOLERANCE}. "
                    f"PubMed reports {py}; Crossref reports {cy}. Both values are reported and "
                    "neither has been replaced. This is a metadata discrepancy, not a failed "
                    "verification.")
        elif year_verdict is False:
            status = STATUS_NOT_VERIFIED
            note = (f"PubMed reports {py} and Crossref reports {cy}, a gap of {year_gap} years, "
                    f"which exceeds the documented online-first tolerance of "
                    f"{ONLINE_FIRST_YEAR_TOLERANCE}. Online-first versus issue dating does not "
                    "account for it, so the disagreement is unexplained. Neither value has been "
                    "altered.")
        elif year_verdict == "WITHIN_TOLERANCE":
            status = STATUS_PARTIALLY_VERIFIED
            note = ("The publication years differ within tolerance, but too little other "
                    "metadata was comparable to establish that these records describe the same "
                    "work.")
        elif comparable and all(v is True for v in comparable):
            status = STATUS_VERIFIED
            note = "Confirmed independently by Crossref and PubMed; compared fields agree."
        else:
            status = STATUS_PARTIALLY_VERIFIED
            note = "Both sources returned a record but too few fields were comparable."
    elif cr_rec:
        status = "PARTIALLY_VERIFIED"
        metadata_match = {"title": None, "year": None, "doi": None}
        note = ("Crossref confirms this DOI exists. Crossref alone is capped at "
                "PARTIALLY_VERIFIED — no PubMed record cross-checked it.")
    elif pm_rec:
        status = "PARTIALLY_VERIFIED"
        metadata_match = {"title": None, "year": None, "doi": None}
        note = ("PubMed returned a record, but it was not cross-checked against Crossref "
                "(no DOI supplied, or the DOI was not found).")
    else:
        status = "NOT_VERIFIED"
        metadata_match = {"title": None, "year": None, "doi": None}
        if doi and cr_status in ("NOT_FOUND", "ZERO_RESULTS"):
            note = ("Crossref has no record for this DOI. The DOI does not resolve — "
                    "the citation could not be verified and may be incorrect or fabricated.")
        else:
            note = ("Neither Crossref nor PubMed returned a matching record. "
                    "The citation could not be verified.")

    return {
        "ok": True,
        "verification_status": status,
        "note": note,
        "doi": out_doi,
        "pmid": out_pmid,
        "title": out_title,
        "journal": out_journal,
        "year": out_year,
        "authors": out_authors,
        "metadata_match": metadata_match,
        # Additive fields (v1.2 RC). Existing keys and the tool name are unchanged, so an older
        # client keeps working; a v1.2 client reads the explicit years and discrepancy type.
        "pubmed_year": year_detail.get("pubmed_year"),
        "crossref_year": year_detail.get("crossref_year"),
        "year_comparison": year_detail.get("year_comparison"),
        "year_gap": year_detail.get("year_gap"),
        "year_tolerance": year_detail.get("year_tolerance", ONLINE_FIRST_YEAR_TOLERANCE),
        "year_source_names": year_detail.get("year_source_names"),
        "discrepancy_type": (DISCREPANCY_ONLINE_FIRST
                             if status == STATUS_VERIFIED_WITH_METADATA_DISCREPANCY else None),
        "sources_consulted": sources or ["crossref" if doi else "pubmed"],
        "query": query_desc,
        "source_provenance": {
            "crossref": {
                "consulted": bool(doi),
                "status": cr_status,
                "source_database": "crossref-works",
            },
            "pubmed": {
                "consulted": True,
                "matched": bool(pm_rec),
                "source_database": "pubmed",
            },
            "retrieved_at": _now(),
        },
        "retrieved_at": _now(),
    }


# --------------------------------------------------------------------------
# Tool 4 — search_clinical_trials
# --------------------------------------------------------------------------

def _normalize_trial(rec, query):
    nct = rec.get("nct_id")
    interventions = rec.get("interventions")
    if interventions:
        norm_int = []
        for i in interventions:
            if isinstance(i, dict):
                norm_int.append({"type": i.get("type"), "name": i.get("name")})
            else:
                norm_int.append({"type": None, "name": str(i)})
        interventions = norm_int

    return {
        "nct_id": nct,
        "title": rec.get("brief_title") or rec.get("official_title"),
        "official_title": rec.get("official_title"),
        "status": rec.get("overall_status"),
        "study_type": rec.get("study_type"),
        "phases": rec.get("phases"),
        "conditions": rec.get("conditions"),
        "interventions": interventions,
        "results_posted": rec.get("has_results"),
        "results_first_post_date": rec.get("results_first_post_date"),
        "enrollment": rec.get("enrollment"),
        "start_date": rec.get("start_date"),
        "completion_date": rec.get("completion_date"),
        "why_stopped": rec.get("why_stopped"),
        "source": "clinicaltrials.gov",
        "retrieved_at": _now(),
        "url": ("https://clinicaltrials.gov/study/%s" % nct) if nct else None,
        "evidence_caveat": REGISTRY_CAVEAT,
        "source_provenance": {
            "source_connector": "clinical_trials",
            "source_database": "clinicaltrials.gov-api-v2",
            "query": query,
            "nct_id": nct,
            "retrieval_status": "SUCCESS",
            "retrieved_at": _now(),
        },
    }


def search_clinical_trials(query, max_results=None):
    if not query or not str(query).strip():
        return _upstream_error("INVALID_INPUT", "query is required and must be non-empty.",
                               "clinical_trials", "clinicaltrials.gov-api-v2", query)

    ct = connector_bridge.get("clinical_trials")
    n = _clamp(max_results)
    res = ct.clinical_trials_search(condition=query, max_results=n)

    status = res.get("status")
    if status not in _OK_STATUSES:
        return _upstream_error(status, res.get("message") or res.get("error"),
                               "clinical_trials", "clinicaltrials.gov-api-v2", query)

    records = res.get("records") or []
    results = [_normalize_trial(r, query) for r in records]
    results = [r for r in results if r.get("nct_id")]  # never emit an unidentified trial

    return {
        "ok": True,
        "status": status,
        "interpretation": (
            "The query executed successfully and matched no registry records."
            if status == "ZERO_RESULTS" else
            "Registry records retrieved."
        ),
        "evidence_caveat": REGISTRY_CAVEAT,
        "total_matched": res.get("total_count"),
        "returned": len(results),
        "results": results,
        "executed_query": res.get("executed_query") or query,
        "source": "clinicaltrials.gov",
        "source_provenance": {
            "source_connector": "clinical_trials",
            "source_database": "clinicaltrials.gov-api-v2",
            "query": query,
            "retrieval_status": status,
            "retrieved_at": _now(),
        },
        "retrieved_at": _now(),
    }


# --------------------------------------------------------------------------
# Health check — cheap reachability only, never a real search
# --------------------------------------------------------------------------

_HEALTH_TARGETS = (
    ("pubmed", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"
               "?db=pubmed&retmode=json&tool=dana_dental_evidence"),
    ("crossref", "https://api.crossref.org/types"),
    ("clinicaltrials.gov", "https://clinicaltrials.gov/api/v2/version"),
)


def health(timeout=5):
    """
    Cheap availability probe. Each target is a small static/metadata endpoint —
    no search is executed, so this cannot be used to burn upstream rate limits.
    """
    import urllib.request
    import urllib.error

    checks = {}
    all_up = True
    for name, url in _HEALTH_TARGETS:
        started = datetime.datetime.now(datetime.timezone.utc)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dental-ai-research-mcp/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
                resp.read(256)  # touch the body, discard it
            up = 200 <= code < 300
            checks[name] = {"available": up, "http_status": code}
        except urllib.error.HTTPError as e:
            up = False
            checks[name] = {"available": False, "http_status": e.code, "error": "HTTP %s" % e.code}
        except Exception as e:
            up = False
            checks[name] = {"available": False, "http_status": None,
                            "error": "%s: %s" % (type(e).__name__, e)}
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
        checks[name]["latency_seconds"] = round(elapsed, 3)
        all_up = all_up and up

    return {
        "status": "ok" if all_up else "degraded",
        "checked_at": _now(),
        "upstream": checks,
        "note": "Reachability probe only — no search is executed by this endpoint.",
    }
