"""
server.py

Remote MCP server for Dental AI research tools — Streamable HTTP transport, WSGI, zero
third-party dependencies.

Why WSGI and not the official SDK
---------------------------------
The official MCP Python SDK requires Python >= 3.10. The build/target environment here is
Python 3.9, and the vendored Dental AI connectors are themselves stdlib-only. Writing the
transport against WSGI keeps the whole server dependency-free, runnable under any standard
Python server (gunicorn, waitress, mod_wsgi) and on any PaaS, which is the smallest possible
production surface. `wsgiref` serves it locally with nothing installed at all.

Transport
---------
Implements the MCP Streamable HTTP transport:

    POST /mcp    JSON-RPC 2.0 request  -> JSON-RPC response (application/json)
    GET  /mcp    -> 405 (this server offers no server-initiated SSE stream, which the
                   spec explicitly permits)
    DELETE /mcp  -> 200 (session teardown is a no-op; this server is stateless)
    GET  /health -> cheap upstream availability probe
    GET  /       -> service descriptor

Statelessness is deliberate: no sessions, no database, no user accounts. Every request is
self-contained, so any number of instances can sit behind a load balancer with no shared
state and nothing to corrupt.
"""
import json
import sys
import traceback

import connector_bridge
import tools

SERVER_NAME = "dental-ai-research"
SERVER_VERSION = "1.0.0"

# Protocol revisions this server can speak. The client's requested version is echoed back
# when supported; otherwise we answer with our preferred one and let the client decide.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL_VERSION = "2025-06-18"

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB; these are small JSON-RPC envelopes

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# --------------------------------------------------------------------------
# Tool registry — exactly four public tools. No more in v1.
# --------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "search_pubmed",
        "title": "Search PubMed",
        "description": (
            "Search PubMed/NCBI for biomedical and dental literature. Returns real PMIDs with "
            "title, authors, journal, publication date, publication type, DOI when available "
            "and abstract when available. Identifiers are never fabricated. A retrieval failure "
            "is reported as an explicit error and must never be read as 'no evidence exists'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms, e.g. 'peri-implantitis surgical treatment'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum records to return (1-50, default 10).",
                    "minimum": 1,
                    "maximum": tools.MAX_RESULTS_CAP,
                },
                "date_from": {
                    "type": "string",
                    "description": "Earliest publication date, 'YYYY' or 'YYYY/MM/DD'.",
                },
                "date_to": {
                    "type": "string",
                    "description": "Latest publication date, 'YYYY' or 'YYYY/MM/DD'.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_systematic_reviews",
        "title": "Search systematic reviews and meta-analyses",
        "description": (
            "Search PubMed restricted to its structured Publication Type field "
            "(\"Systematic Review\"[pt] OR \"Meta-Analysis\"[pt]) — never title text. "
            "This is PubMed coverage only. It is NOT Cochrane, Embase or Scopus access, and "
            "must not be presented as a Cochrane search; those sources are NOT IMPLEMENTED."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Clinical question or topic to search.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum records to return (1-50, default 10).",
                    "minimum": 1,
                    "maximum": tools.MAX_RESULTS_CAP,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify_citation",
        "title": "Verify a citation",
        "description": (
            "Verify a citation against Crossref and PubMed. Accepts any combination of DOI, "
            "PMID and title. Returns verification_status (VERIFIED / PARTIALLY_VERIFIED / "
            "NOT_VERIFIED) with the metadata fields that were actually compared. A DOI that "
            "does not resolve returns NOT_VERIFIED — a real finding, distinct from an upstream "
            "failure. No identifier is ever invented."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doi": {"type": "string", "description": "DOI, e.g. '10.1016/j.prosdent.2019.05.021'."},
                "pmid": {"type": "string", "description": "PubMed ID, e.g. '31474292'."},
                "title": {"type": "string", "description": "Article title, used when no identifier is available."},
            },
            "anyOf": [
                {"required": ["doi"]},
                {"required": ["pmid"]},
                {"required": ["title"]},
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_clinical_trials",
        "title": "Search ClinicalTrials.gov",
        "description": (
            "Search the ClinicalTrials.gov registry (API v2) for registered studies. Returns "
            "real NCT IDs with title, recruitment status, study type, conditions, interventions "
            "and whether results were posted. A registry record documents that a study was "
            "registered — it is NOT evidence that an intervention works."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Condition or topic, e.g. 'peri-implantitis'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum records to return (1-50, default 10).",
                    "minimum": 1,
                    "maximum": tools.MAX_RESULTS_CAP,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

TOOL_IMPLEMENTATIONS = {
    "search_pubmed": tools.search_pubmed,
    "search_systematic_reviews": tools.search_systematic_reviews,
    "verify_citation": tools.verify_citation,
    "search_clinical_trials": tools.search_clinical_trials,
}

assert len(TOOL_DEFINITIONS) == 4, "v1 exposes exactly four public tools"
assert {t["name"] for t in TOOL_DEFINITIONS} == set(TOOL_IMPLEMENTATIONS), "registry mismatch"


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _handle_initialize(req_id, params):
    requested = (params or {}).get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PREFERRED_PROTOCOL_VERSION
    return _result(req_id, {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "Dental AI research tools. Four public tools: search_pubmed, "
            "search_systematic_reviews, verify_citation, search_clinical_trials. "
            "All identifiers returned are real and retrieved upstream — never fabricate a "
            "PMID, DOI or NCT ID. If a tool returns ok=false, that is a retrieval failure, "
            "not a finding of 'no evidence'. Cochrane, Embase and Scopus are NOT IMPLEMENTED; "
            "do not imply access to them."
        ),
    })


def _handle_tools_list(req_id, _params):
    return _result(req_id, {"tools": TOOL_DEFINITIONS})


def _handle_tools_call(req_id, params):
    params = params or {}
    name = params.get("name")
    args = params.get("arguments") or {}

    impl = TOOL_IMPLEMENTATIONS.get(name)
    if impl is None:
        return _error(req_id, INVALID_PARAMS, "Unknown tool: %s" % name)
    if not isinstance(args, dict):
        return _error(req_id, INVALID_PARAMS, "arguments must be an object")

    try:
        payload = impl(**args)
    except TypeError as exc:
        return _error(req_id, INVALID_PARAMS, "Invalid arguments for %s: %s" % (name, exc))
    except Exception as exc:
        sys.stderr.write("tool %s crashed: %s\n%s\n" % (name, exc, traceback.format_exc()))
        # Surface as a tool error, not a transport error: the model should see and report it.
        payload = {
            "ok": False,
            "status": "INTERNAL_ERROR",
            "error": "%s: %s" % (type(exc).__name__, exc),
            "interpretation": (
                "The tool failed internally. This is NOT a finding of 'no evidence'."
            ),
        }

    text = json.dumps(payload, indent=2, ensure_ascii=False)
    is_error = not payload.get("ok", True)
    return _result(req_id, {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": is_error,
    })


_METHODS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": lambda req_id, _p: _result(req_id, {}),
}

# Notifications carry no id and get no response body.
_NOTIFICATIONS = ("notifications/initialized", "notifications/cancelled")


def handle_message(msg):
    """Dispatch one JSON-RPC message. Returns a response dict, or None for a notification."""
    if not isinstance(msg, dict):
        return _error(None, INVALID_REQUEST, "Request must be a JSON object")
    if msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id"), INVALID_REQUEST, "jsonrpc must be '2.0'")

    method = msg.get("method")
    req_id = msg.get("id")

    if method in _NOTIFICATIONS or (req_id is None and method not in _METHODS):
        return None

    handler = _METHODS.get(method)
    if handler is None:
        return _error(req_id, METHOD_NOT_FOUND, "Unknown method: %s" % method)

    try:
        return handler(req_id, msg.get("params"))
    except Exception as exc:
        sys.stderr.write("handler error: %s\n%s\n" % (exc, traceback.format_exc()))
        return _error(req_id, INTERNAL_ERROR, "Internal error: %s" % exc)


# --------------------------------------------------------------------------
# WSGI application
# --------------------------------------------------------------------------

_JSON = [("Content-Type", "application/json")]


def _respond(start_response, status, payload, extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = list(_JSON) + [("Content-Length", str(len(body)))]
    # Permit browser-based clients; this server exposes only public research data.
    headers += [
        ("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Headers", "Content-Type, Mcp-Session-Id, MCP-Protocol-Version"),
        ("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS"),
        ("Access-Control-Expose-Headers", "Mcp-Session-Id"),
    ]
    if extra_headers:
        headers += extra_headers
    start_response(status, headers)
    return [body]


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/") or "/"
    path = path.rstrip("/") or "/"

    if method == "OPTIONS":
        return _respond(start_response, "204 No Content", {})

    if path == "/health" and method == "GET":
        report = tools.health()
        status = "200 OK" if report["status"] == "ok" else "503 Service Unavailable"
        return _respond(start_response, status, report)

    if path == "/" and method == "GET":
        return _respond(start_response, "200 OK", {
            "service": SERVER_NAME,
            "version": SERVER_VERSION,
            "transport": "mcp-streamable-http",
            "mcp_endpoint": "/mcp",
            "health_endpoint": "/health",
            "tools": [t["name"] for t in TOOL_DEFINITIONS],
            "auth": "none — all four sources are public research APIs",
        })

    if path == "/mcp":
        if method == "GET":
            # No server-initiated stream. The spec allows 405 here.
            return _respond(start_response, "405 Method Not Allowed",
                            {"error": "This server does not offer an SSE stream. POST to /mcp."})
        if method == "DELETE":
            return _respond(start_response, "200 OK", {"ok": True})
        if method != "POST":
            return _respond(start_response, "405 Method Not Allowed", {"error": "Use POST."})

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return _respond(start_response, "413 Payload Too Large",
                            _error(None, INVALID_REQUEST, "Request body too large"))

        raw = environ["wsgi.input"].read(length) if length > 0 else b""
        try:
            msg = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError) as exc:
            return _respond(start_response, "400 Bad Request",
                            _error(None, PARSE_ERROR, "Invalid JSON: %s" % exc))

        if msg is None:
            return _respond(start_response, "400 Bad Request",
                            _error(None, INVALID_REQUEST, "Empty request body"))

        # A JSON-RPC batch is a list; handle it, dropping notification slots.
        if isinstance(msg, list):
            out = [r for r in (handle_message(m) for m in msg) if r is not None]
            if not out:
                return _respond(start_response, "202 Accepted", {})
            return _respond(start_response, "200 OK", out)

        response = handle_message(msg)
        if response is None:
            return _respond(start_response, "202 Accepted", {})
        return _respond(start_response, "200 OK", response)

    return _respond(start_response, "404 Not Found", {"error": "Not found: %s" % path})


app = application  # conventional alias for gunicorn/uvicorn

# Load the connectors at import time, not on first request.
#
# Under a WSGI server (gunicorn on Render) `main()` never runs, so without this the three
# connectors would import lazily inside the first request a dentist makes — turning a
# packaging fault into a mysterious runtime error mid-query instead of a failed boot that
# Render would catch and roll back. Importing here makes a broken deploy fail loudly and
# immediately, and keeps the first real request as fast as every later one.
connector_bridge.load_all()


def main():
    """Local development server. Production uses a real WSGI server — see DEPLOY.md."""
    import argparse
    import os
    from wsgiref.simple_server import make_server

    ap = argparse.ArgumentParser(description="Dental AI Research MCP server")
    ap.add_argument("--host", default="127.0.0.1")
    # Honour $PORT so the same entry point works on any PaaS, Render included.
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()
    sys.stderr.write("Dental AI Research MCP on http://%s:%d/mcp\n" % (args.host, args.port))
    make_server(args.host, args.port, application).serve_forever()


if __name__ == "__main__":
    main()
