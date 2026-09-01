"""
app.py

WSGI entry point shim.

Why this file exists
--------------------
Render reads `render.yaml` only through its **Blueprint** flow (New -> Blueprint). A service
created through the ordinary **New -> Web Service** flow ignores `render.yaml` entirely and
falls back to Render's default Python start command:

    gunicorn app:app

With no `app.py` at the repository root that fails at boot with:

    ModuleNotFoundError: No module named 'app'

The real application lives in `server.py` and is unchanged. This module simply re-exports it
under the name Render's default command expects, so the service boots correctly whether it
was created as a Blueprint (`gunicorn server:app`, per render.yaml) or as a plain Web
Service (`gunicorn app:app`, Render's default).

This is a deployment-path shim only. It adds no dependency, defines no new behaviour, and
changes nothing about the four MCP tools or the PubMed / Crossref / ClinicalTrials.gov
retrieval logic — both names point at the same validated callable.
"""
from server import application

# Render's default start command resolves `app:app`; gunicorn also accepts `app:application`.
app = application

__all__ = ["app", "application"]
