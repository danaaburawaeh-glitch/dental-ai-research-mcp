"""
tests/test_mcp.py

Test suite for the Dental AI Research MCP server.

Covers the nine required cases:
  1. PubMed search returns real PMIDs
  2. Systematic review search returns filtered real records
  3. Crossref verifies a real DOI
  4. ClinicalTrials.gov returns real NCT records
  5. Invalid DOI returns NOT_VERIFIED
  6. Network/upstream failure produces an explicit error
  7. No fabricated identifiers
  8. MCP protocol conformance (initialize / tools/list / tools/call)
  9. All four tool schemas validate

Live tests hit real public APIs. Failure-path tests use monkeypatching so they are
deterministic and do not require breaking the network.

Run:  python3 tests/test_mcp.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import connector_bridge  # noqa: E402
import server  # noqa: E402
import tools  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  PASS  %s" % label)
    else:
        FAIL += 1
        FAILURES.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def section(name):
    print("\n=== %s ===" % name)


# --------------------------------------------------------------------------
# WSGI helper — drives the real application, no server socket needed
# --------------------------------------------------------------------------

def wsgi_call(method, path, body=None):
    import io
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    chunks = server.application(environ, start_response)
    payload = b"".join(chunks).decode("utf-8")
    try:
        parsed = json.loads(payload) if payload else None
    except ValueError:
        parsed = None
    return captured.get("status"), parsed


def rpc(method, params=None, req_id=1):
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return wsgi_call("POST", "/mcp", msg)


def call_tool(name, args):
    status, resp = rpc("tools/call", {"name": name, "arguments": args})
    if not resp or "result" not in resp:
        return status, resp, None
    return status, resp, resp["result"].get("structuredContent")


# --------------------------------------------------------------------------
# 1. MCP protocol conformance
# --------------------------------------------------------------------------

def test_protocol():
    section("MCP protocol conformance")

    status, resp = rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    })
    check("initialize returns 200", status == "200 OK", status)
    r = (resp or {}).get("result", {})
    check("initialize echoes supported protocolVersion",
          r.get("protocolVersion") == "2025-06-18", r.get("protocolVersion"))
    check("initialize advertises tools capability", "tools" in r.get("capabilities", {}))
    check("initialize reports serverInfo.name",
          r.get("serverInfo", {}).get("name") == "dental-ai-research")

    # Unknown protocol version must not crash; server answers with its preferred one.
    _, resp2 = rpc("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    check("unsupported protocolVersion negotiates down cleanly",
          resp2["result"]["protocolVersion"] == server.PREFERRED_PROTOCOL_VERSION)

    # initialized notification -> no response body, 202
    status, _ = wsgi_call("POST", "/mcp",
                          {"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("notifications/initialized accepted with no response", status == "202 Accepted", status)

    status, resp = rpc("ping")
    check("ping works", status == "200 OK" and "result" in (resp or {}))

    status, resp = rpc("nonexistent/method")
    check("unknown method -> METHOD_NOT_FOUND",
          resp.get("error", {}).get("code") == server.METHOD_NOT_FOUND)

    status, _ = wsgi_call("GET", "/mcp")
    check("GET /mcp -> 405 (no SSE stream offered)", status == "405 Method Not Allowed", status)

    # Malformed JSON
    import io
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp",
               "CONTENT_LENGTH": "5", "wsgi.input": io.BytesIO(b"{bad}")}
    cap = {}
    server.application(environ, lambda s, h: cap.update(status=s))
    check("malformed JSON -> 400 PARSE_ERROR", cap["status"] == "400 Bad Request", cap["status"])


# --------------------------------------------------------------------------
# 2. Tool schemas
# --------------------------------------------------------------------------

def test_schemas():
    section("Tool schemas")

    status, resp = rpc("tools/list")
    check("tools/list returns 200", status == "200 OK")
    tool_list = resp["result"]["tools"]
    names = [t["name"] for t in tool_list]

    check("exactly 4 public tools", len(tool_list) == 4, names)
    check("tool names are exactly the agreed four",
          set(names) == {"search_pubmed", "search_systematic_reviews",
                         "verify_citation", "search_clinical_trials"}, names)

    for t in tool_list:
        n = t["name"]
        check("%s has description" % n, bool(t.get("description")))
        s = t.get("inputSchema")
        check("%s has object inputSchema" % n, isinstance(s, dict) and s.get("type") == "object")
        check("%s schema has properties" % n, bool(s.get("properties")))
        # every schema must be serializable and free of unsupported constructs
        try:
            json.dumps(s)
            ok = True
        except (TypeError, ValueError):
            ok = False
        check("%s schema is JSON-serializable" % n, ok)

    sr = next(t for t in tool_list if t["name"] == "search_systematic_reviews")
    check("systematic-review tool disclaims Cochrane/Embase/Scopus",
          "NOT IMPLEMENTED" in sr["description"] and "Cochrane" in sr["description"])

    ct = next(t for t in tool_list if t["name"] == "search_clinical_trials")
    check("clinical-trials tool states registry != efficacy",
          "NOT evidence" in ct["description"])

    vc = next(t for t in tool_list if t["name"] == "verify_citation")
    check("verify_citation accepts doi/pmid/title",
          set(vc["inputSchema"]["properties"]) == {"doi", "pmid", "title"})


# --------------------------------------------------------------------------
# 3. Live retrieval
# --------------------------------------------------------------------------

PMID_RE = re.compile(r"^\d{1,8}$")
NCT_RE = re.compile(r"^NCT\d{8}$")
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def test_pubmed_live():
    section("PubMed search (live)")
    _, _, out = call_tool("search_pubmed", {"query": "peri-implantitis treatment",
                                            "max_results": 5})
    check("ok", out and out.get("ok") is True, out and out.get("error"))
    if not (out and out.get("ok")):
        return
    res = out["results"]
    check("returned records", len(res) > 0, len(res))
    check("respects max_results", len(res) <= 5, len(res))
    check("every record has a real PMID",
          all(PMID_RE.match(str(r["pmid"] or "")) for r in res),
          [r.get("pmid") for r in res])
    check("every record has a title", all(r.get("title") for r in res))
    check("contract fields present",
          all(set(("pmid", "title", "authors", "journal", "publication_date",
                   "publication_type", "doi", "abstract", "source",
                   "retrieved_at")) <= set(r) for r in res))
    check("source is pubmed", all(r["source"] == "pubmed" for r in res))
    check("provenance on every record", all(r.get("source_provenance") for r in res))
    check("DOIs, where present, are well formed",
          all(DOI_RE.match(r["doi"]) for r in res if r.get("doi")),
          [r.get("doi") for r in res])
    print("     e.g. PMID %s — %s" % (res[0]["pmid"], (res[0]["title"] or "")[:62]))


def test_systematic_reviews_live():
    section("Systematic review search (live, filtered)")
    _, _, out = call_tool("search_systematic_reviews",
                          {"query": "peri-implantitis treatment", "max_results": 5})
    check("ok", out and out.get("ok") is True, out and out.get("error"))
    if not (out and out.get("ok")):
        return
    res = out["results"]
    check("returned records", len(res) > 0, len(res))
    check("every record has a real PMID",
          all(PMID_RE.match(str(r["pmid"] or "")) for r in res))
    check("PublicationType filter was applied",
          "Systematic Review" in (out.get("executed_query") or "")
          and "Meta-Analysis" in (out.get("executed_query") or ""),
          out.get("executed_query"))

    # The filter is structural: every returned record should carry an SR/MA publication type.
    def is_sr(r):
        pts = r.get("publication_type") or []
        return any(("systematic review" in p.lower() or "meta-analysis" in p.lower())
                   for p in pts)

    check("records actually carry SR/MA publication types",
          all(is_sr(r) for r in res),
          [r.get("publication_type") for r in res])
    check("does not imply Cochrane access",
          out.get("cochrane_status") == "NOT IMPLEMENTED"
          and "NOT Cochrane" in out.get("coverage_caveat", ""))
    print("     e.g. PMID %s — %s" % (res[0]["pmid"], (res[0]["title"] or "")[:62]))


def test_crossref_live():
    section("Crossref citation verification (live)")
    _, _, out = call_tool("verify_citation",
                          {"doi": "10.1016/j.prosdent.2019.05.021"})
    check("ok", out and out.get("ok") is True, out and out.get("error"))
    if not (out and out.get("ok")):
        return
    check("status is VERIFIED or PARTIALLY_VERIFIED",
          out["verification_status"] in ("VERIFIED", "PARTIALLY_VERIFIED"),
          out["verification_status"])
    check("DOI echoed from upstream record",
          (out.get("doi") or "").lower() == "10.1016/j.prosdent.2019.05.021")
    check("title resolved", bool(out.get("title")))
    check("journal resolved", bool(out.get("journal")))
    check("year resolved", bool(out.get("year")))
    check("authors resolved", bool(out.get("authors")))
    check("metadata_match present", isinstance(out.get("metadata_match"), dict))
    check("source_provenance present", bool(out.get("source_provenance")))
    print("     %s (%s) — %s" % (out.get("title", "")[:50], out.get("year"),
                                 out["verification_status"]))


def test_invalid_doi():
    section("Invalid DOI -> NOT_VERIFIED")
    _, _, out = call_tool("verify_citation",
                          {"doi": "10.9999/this-doi-does-not-exist-xyz123"})
    check("call succeeded (not an upstream error)", out and out.get("ok") is True)
    check("verification_status is NOT_VERIFIED",
          out.get("verification_status") == "NOT_VERIFIED", out.get("verification_status"))
    check("no DOI fabricated in output", out.get("doi") is None, out.get("doi"))
    check("no PMID fabricated in output", out.get("pmid") is None, out.get("pmid"))
    check("explains the DOI did not resolve", "does not resolve" in (out.get("note") or ""))


def test_clinical_trials_live():
    section("ClinicalTrials.gov search (live)")
    _, _, out = call_tool("search_clinical_trials",
                          {"query": "peri-implantitis", "max_results": 5})
    check("ok", out and out.get("ok") is True, out and out.get("error"))
    if not (out and out.get("ok")):
        return
    res = out["results"]
    check("returned records", len(res) > 0, len(res))
    check("every record has a real NCT ID",
          all(NCT_RE.match(str(r["nct_id"] or "")) for r in res),
          [r.get("nct_id") for r in res])
    check("contract fields present",
          all(set(("nct_id", "title", "status", "study_type", "conditions",
                   "interventions", "results_posted", "source", "retrieved_at"))
              <= set(r) for r in res))
    check("registry != efficacy caveat on response",
          "NOT evidence" in out.get("evidence_caveat", ""))
    check("registry != efficacy caveat on every record",
          all("NOT evidence" in (r.get("evidence_caveat") or "") for r in res))
    check("source is clinicaltrials.gov", all(r["source"] == "clinicaltrials.gov" for r in res))
    print("     e.g. %s — %s" % (res[0]["nct_id"], (res[0]["title"] or "")[:60]))


# --------------------------------------------------------------------------
# 4. Failure handling
# --------------------------------------------------------------------------

def test_upstream_failure():
    section("Upstream failure -> explicit error (never 'no evidence')")

    pm = connector_bridge.get("pubmed")
    original = pm.pubmed_search

    def broken(*a, **k):
        return {"status": "UPSTREAM_ERROR", "message": "simulated NCBI outage"}

    pm.pubmed_search = broken
    try:
        _, _, out = call_tool("search_pubmed", {"query": "dental implant"})
    finally:
        pm.pubmed_search = original

    check("ok is False", out.get("ok") is False, out.get("ok"))
    check("status is UPSTREAM_ERROR", out.get("status") == "UPSTREAM_ERROR", out.get("status"))
    check("results is null, not an empty list",
          out.get("results") is None, out.get("results"))
    check("explicitly says this is NOT 'no evidence'",
          "NOT a finding of 'no evidence'" in out.get("interpretation", ""),
          out.get("interpretation"))
    check("error message preserved", "simulated NCBI outage" in out.get("error", ""))
    check("provenance retained on failure", bool(out.get("source_provenance")))

    # Timeout path
    pm.pubmed_search = lambda *a, **k: {"status": "TIMEOUT", "message": "timed out"}
    try:
        _, _, out2 = call_tool("search_pubmed", {"query": "x"})
    finally:
        pm.pubmed_search = original
    check("TIMEOUT surfaces as explicit failure",
          out2.get("ok") is False and out2.get("status") == "TIMEOUT")

    # A tool crash must not take down the transport
    pm.pubmed_search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        status, resp, out3 = call_tool("search_pubmed", {"query": "x"})
    finally:
        pm.pubmed_search = original
    check("internal crash still returns valid JSON-RPC 200", status == "200 OK", status)
    check("crash is reported as a tool error", out3.get("ok") is False)
    check("crash marked isError for the model", resp["result"].get("isError") is True)


def test_zero_results_distinct_from_failure():
    section("ZERO_RESULTS is distinct from failure")
    # NOTE: a bare nonsense token is NOT a zero-result query. PubMed's term translation
    # silently drops unmatchable words, so "zzzqqx... dentistry" matches ~822k records on
    # "dentistry" alone. Field-tagging the nonsense token is what actually forces zero.
    _, _, out = call_tool("search_pubmed",
                          {"query": '"zzzqqxunlikelyterm123456"[Title]'})
    check("ok is True (query ran fine)", out.get("ok") is True, out.get("status"))
    check("status is ZERO_RESULTS", out.get("status") == "ZERO_RESULTS", out.get("status"))
    check("results is an empty list, not null", out.get("results") == [])
    check("does not claim absence of evidence",
          "does NOT establish" in out.get("interpretation", ""))


def test_no_fabricated_identifiers():
    section("No fabricated identifiers")

    pm = connector_bridge.get("pubmed")
    orig_search, orig_fetch = pm.pubmed_search, pm.pubmed_fetch

    # Upstream returns a record with NO pmid — it must be dropped, not given one.
    pm.pubmed_search = lambda *a, **k: {"status": "SUCCESS", "pmids": ["1"], "count": 1,
                                        "raw_query": "q", "query_translation": "q"}
    pm.pubmed_fetch = lambda *a, **k: {"status": "SUCCESS", "records": [
        {"pmid": None, "title": "Record with no identifier", "source": "pubmed"},
    ]}
    try:
        _, _, out = call_tool("search_pubmed", {"query": "x"})
    finally:
        pm.pubmed_search, pm.pubmed_fetch = orig_search, orig_fetch

    check("record lacking a PMID is dropped, never assigned one",
          out.get("results") == [], out.get("results"))

    ct = connector_bridge.get("clinical_trials")
    orig_ct = ct.clinical_trials_search
    ct.clinical_trials_search = lambda *a, **k: {"status": "SUCCESS", "total_count": 1,
                                                  "records": [{"nct_id": None,
                                                               "brief_title": "No NCT"}]}
    try:
        _, _, out2 = call_tool("search_clinical_trials", {"query": "x"})
    finally:
        ct.clinical_trials_search = orig_ct
    check("trial lacking an NCT ID is dropped, never assigned one",
          out2.get("results") == [], out2.get("results"))

    # verify_citation with a title that matches nothing must not invent identifiers
    _, _, out3 = call_tool("verify_citation",
                           {"title": "Zzq Unlikely Nonexistent Dental Title 998877"})
    check("unmatched title yields no invented DOI", out3.get("doi") is None, out3.get("doi"))
    check("unmatched title yields no invented PMID", out3.get("pmid") is None, out3.get("pmid"))


def test_input_validation():
    section("Input validation")
    _, _, out = call_tool("search_pubmed", {"query": "   "})
    check("empty query rejected", out.get("ok") is False and out.get("status") == "INVALID_INPUT")

    _, _, out = call_tool("verify_citation", {})
    check("verify_citation with no identifier rejected",
          out.get("ok") is False and out.get("status") == "INVALID_INPUT")

    status, resp = rpc("tools/call", {"name": "search_sfda", "arguments": {}})
    check("unknown tool rejected (SFDA is out of scope)",
          resp.get("error", {}).get("code") == server.INVALID_PARAMS)

    _, _, out = call_tool("search_pubmed", {"query": "dental", "max_results": 9999})
    check("max_results clamped to cap",
          out.get("ok") is True and len(out.get("results") or []) <= tools.MAX_RESULTS_CAP,
          len(out.get("results") or []))


def test_health():
    section("Health endpoint")
    status, report = wsgi_call("GET", "/health")
    check("health responds", status in ("200 OK", "503 Service Unavailable"), status)
    check("reports all three upstreams",
          set(report["upstream"]) == {"pubmed", "crossref", "clinicaltrials.gov"},
          list(report.get("upstream", {})))
    check("states it runs no searches", "no search is executed" in report.get("note", ""))
    for name, c in report["upstream"].items():
        print("     %-20s available=%-5s http=%s  %.3fs"
              % (name, c["available"], c["http_status"], c["latency_seconds"]))
    check("all three upstreams available", report["status"] == "ok", report["status"])


def test_no_secrets():
    section("Security — no secrets in source or responses")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    pattern = re.compile(
        r"(api[_-]?key|secret|token|password)\s*=\s*[\"'][A-Za-z0-9_\-]{8,}[\"']", re.I)
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in ("__pycache__", ".git")]
        for f in fn:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            for i, line in enumerate(open(p, encoding="utf-8", errors="ignore"), 1):
                if pattern.search(line):
                    bad.append("%s:%d" % (os.path.relpath(p, root), i))
    check("no hard-coded credential literal in any source file", not bad, bad)
    check("SFDA connector not vendored (out of scope)",
          not os.path.isdir(os.path.join(root, "connectors", "sfda")))


def main():
    print("Dental AI Research MCP — test suite")
    connector_bridge.load_all()

    test_protocol()
    test_schemas()
    test_pubmed_live()
    test_systematic_reviews_live()
    test_crossref_live()
    test_invalid_doi()
    test_clinical_trials_live()
    test_upstream_failure()
    test_zero_results_distinct_from_failure()
    test_no_fabricated_identifiers()
    test_input_validation()
    test_health()
    test_no_secrets()

    print("\n" + "=" * 58)
    print("%d/%d passed" % (PASS, PASS + FAIL))
    if FAILURES:
        print("FAILED:")
        for f in FAILURES:
            print("  - %s" % f)
    print("=" * 58)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
