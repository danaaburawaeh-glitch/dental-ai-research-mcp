"""
connector_bridge.py

Loads the three vendored Dental AI v1.0.2 connectors in-process.

Why this file exists
--------------------
The connectors were written as standalone CLIs. Each one does:

    sys.path.insert(0, <connectors/>)      # so `shared.*` resolves
    from errors import ...                 # top-level, relative to its OWN directory
    from parser import ...
    from models import ...
    from rate_limit import ...

That works when a single connector owns the process. It does not work when three of them
live in one server: `pubmed/errors.py`, `crossref/errors.py` and `clinical_trials/errors.py`
would all claim `sys.modules["errors"]`, and whichever imported first would silently win.
That is a real correctness bug, not a style problem — the wrong status taxonomy would be
applied to the wrong connector.

This bridge loads each connector under its own private module namespace, purging the
colliding short names between loads so each client binds to its own siblings. The
connector source itself is UNMODIFIED — that is the point: the retrieval logic is the
validated v1.0.2 code, byte for byte.
"""
import importlib.util
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONNECTORS = os.path.join(_HERE, "connectors")

# Short module names each connector package imports relative to its own directory.
_COLLIDING = ("errors", "parser", "models", "rate_limit", "client")

_loaded = {}
_lock = threading.Lock()


def _load(connector):
    """Import <connectors>/<connector>/client.py under a private namespace."""
    pkg_dir = os.path.join(_CONNECTORS, connector)
    client_py = os.path.join(pkg_dir, "client.py")
    if not os.path.isfile(client_py):
        raise RuntimeError("vendored connector missing: %s" % client_py)

    saved = {n: sys.modules.pop(n, None) for n in _COLLIDING}
    saved_path = list(sys.path)
    try:
        # The connector's own directory first, then connectors/ for `shared.*`.
        sys.path.insert(0, _CONNECTORS)
        sys.path.insert(0, pkg_dir)

        spec = importlib.util.spec_from_file_location(
            "dental_mcp_%s_client" % connector, client_py
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = saved_path
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


def get(connector):
    """Return the loaded client module for 'pubmed' | 'crossref' | 'clinical_trials'."""
    with _lock:
        if connector not in _loaded:
            _loaded[connector] = _load(connector)
        return _loaded[connector]


def load_all():
    """Eagerly load all three. Called at server start so an import fault fails loudly at boot."""
    for c in ("pubmed", "crossref", "clinical_trials"):
        get(c)
