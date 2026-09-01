# Deployment

**Recommendation: Render, Starter instance.**

One option, not five. Here is why it wins for this specific server, and exactly how to do it.

---

## Why Render

This server is a stateless, zero-dependency WSGI app. That makes the deciding factors
setup cost and maintenance cost, not scale or features.

| Requirement | How Render meets it |
|---|---|
| Public HTTPS | Automatic TLS on `https://<name>.onrender.com`. No certificate work, no renewal |
| Low maintenance | No Docker, no CLI, no cloud project, no IAM. `render.yaml` is already in this repo |
| Reliable availability | Starter instances do **not** spin down. Built-in health checks poll `/health` and restart a bad instance |
| Easy updates | Connect the GitHub repo once; every `git push` redeploys automatically |

**Cost: $7/month** (Starter).

**Do not use the free tier for this.** Free instances spin down after ~15 minutes idle and
cold-start in roughly 50 seconds. A dentist mid-consultation would watch a research query
hang. The $7 buys away the one failure mode that actually matters here.

Cloud Run is the credible alternative and is genuinely cheaper at this volume, but it
requires Docker, `gcloud` and a GCP project — materially more setup and more to maintain
for no benefit at this size. If this ever needs to scale beyond one small instance,
revisit it then.

---

## Steps

**1. Push this directory to a GitHub repository.**

Use a **new, separate** repo — not the Dental AI plugin repo. The plugin is a published
product at v1.0.2; this is unrelated infrastructure and must not disturb it.

```bash
cd ~/Downloads/DENTAL-AI-RESEARCH-MCP
git init
git add .
git commit -m "Dental AI Research MCP v1.0.0 — four public research tools"
git branch -M main
git remote add origin https://github.com/<you>/dental-ai-research-mcp.git
git push -u origin main
```

**2. Create the service.**

In the Render dashboard: **New → Web Service → connect the repo**. `render.yaml` is
detected automatically and supplies the runtime, build command, start command and health
check path. Confirm the plan is **Starter**.

**3. Wait for the first deploy, then verify.**

```bash
curl -s https://<your-service>.onrender.com/health | python3 -m json.tool
```

Expect `"status": "ok"` with all three upstreams `"available": true`.

**4. (Optional) Add an NCBI API key.**

Dashboard → Environment → add `NCBI_API_KEY` as a secret. It raises the PubMed rate limit
from 3 to 10 requests/second. Not required; the server works without it. Never commit the
value.

---

## Connect it to Claude

Both clients take the same URL: `https://<your-service>.onrender.com/mcp`

**Claude Web** — Settings → Connectors → *Add custom connector* → paste the URL.

**Claude Desktop** — Settings → Connectors → *Add custom connector* → paste the URL.

No authentication step: all four sources are public research APIs, so the server exposes
no auth and holds no secrets.

---

## Operating notes

**Health check.** Render polls `/health`, which probes three small metadata endpoints
(PubMed `einfo`, Crossref `/types`, ClinicalTrials.gov `/version`). No searches are run, so
health polling cannot consume upstream rate limit. `/health` returns `503` when any
upstream is unreachable, so a genuine outage is visible rather than silent.

**Statelessness.** No database, no sessions, no user accounts, no stored patient data.
Every request is self-contained, so instances can be added or restarted freely and there is
nothing to back up or migrate.

**Rate limits.** The vendored connectors carry their own per-attempt rate limiting and
bounded retry with backoff. Upstream `429`s surface as an explicit `RATE_LIMITED` failure —
never as an empty result set.

**What to watch.** The one dependency worth monitoring is upstream availability. If NCBI or
ClinicalTrials.gov has an outage, `/health` reports `degraded` and tools return explicit
upstream errors. That is correct behaviour, not a bug to patch around: the server never
converts an outage into a claim about the literature.

---

## Not done yet

Deployment is **not** part of this task, and nothing has been deployed. The next step after
deployment would be wiring the plugin to the live endpoint — also **not** started. The
public Dental AI v1.0.2 plugin remains unmodified.
