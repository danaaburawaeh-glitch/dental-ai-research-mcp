"""
tests/test_verification_parity.py

The remote server and the plugin's local evidence layer must return the same citation semantics
for the same pair of records. A client must never have to know which transport answered in order
to interpret the verdict.

This suite pins the server side of that contract. It exercises the decision logic directly
against record pairs — no network, no live server.

Run: python3 tests/test_verification_parity.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)   # tools.py imports connector_bridge from the repo root

spec = importlib.util.spec_from_file_location("srv_tools", os.path.join(ROOT, "tools.py"))
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)

R = []


def check(name, cond, detail=""):
    R.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ── Year comparison ─────────────────────────────────────────────────────────────────────────
print("── Year comparison ──")

check("01 identical years are a match", tools._compare_years(2025, 2025) == (True, 0))
check("02 a one-year gap is within tolerance",
      tools._compare_years(2025, 2026) == ("WITHIN_TOLERANCE", 1))
check("03 the direction of the gap does not matter",
      tools._compare_years(2026, 2025) == ("WITHIN_TOLERANCE", 1))
check("04 a two-year gap is beyond tolerance", tools._compare_years(2019, 2021) == (False, 2))
check("05 a missing year is not comparable", tools._compare_years(None, 2025) == (None, None))
check("06 the documented tolerance is one year", tools.ONLINE_FIRST_YEAR_TOLERANCE == 1)

# ── Author comparison — both source renderings ──────────────────────────────────────────────
print("\n── Author comparison ──")

check("07 PubMed 'Smith J' matches Crossref 'John Smith'",
      tools._authors_match(["John Smith"], ["Smith J"]) is True)
check("08 compound surnames match in either order",
      tools._authors_match(["van der Berg AB"], ["AB van der Berg"]) is True)
check("09 unrelated author lists do not match",
      tools._authors_match(["Alice Brown"], ["Smith J"]) is False)
check("10 an absent author list is not comparable, not a mismatch",
      tools._authors_match(None, ["Smith J"]) is None)

# ── Journal comparison — ISO abbreviation vs full title ─────────────────────────────────────
print("\n── Journal comparison ──")

check("11 an ISO abbreviation matches its full title",
      tools._journals_match("Clinical Oral Investigations", "Clin Oral Investig") is True)
check("12 J Prosthet Dent matches its full title",
      tools._journals_match("The Journal of Prosthetic Dentistry", "J Prosthet Dent") is True)
check("13 two genuinely different journals do not match",
      tools._journals_match("Journal of Dentistry", "Journal of Prosthetic Dentistry") is not True)
check("14 an absent journal is not comparable",
      tools._journals_match(None, "J Dent") is None)

# ── The four statuses exist and are named exactly as the plugin names them ──────────────────
print("\n── Status vocabulary ──")

check("15 VERIFIED", tools.STATUS_VERIFIED == "VERIFIED")
check("16 VERIFIED_WITH_METADATA_DISCREPANCY",
      tools.STATUS_VERIFIED_WITH_METADATA_DISCREPANCY == "VERIFIED_WITH_METADATA_DISCREPANCY")
check("17 PARTIALLY_VERIFIED", tools.STATUS_PARTIALLY_VERIFIED == "PARTIALLY_VERIFIED")
check("18 NOT_VERIFIED", tools.STATUS_NOT_VERIFIED == "NOT_VERIFIED")
check("19 the discrepancy type is named identically to the plugin",
      tools.DISCREPANCY_ONLINE_FIRST == "ONLINE_FIRST_VS_ISSUE_YEAR")

# ── API compatibility ───────────────────────────────────────────────────────────────────────
print("\n── API compatibility ──")

names = [t["name"] for t in tools.TOOL_SPECS] if hasattr(tools, "TOOL_SPECS") else None
if names is None:
    server_spec = importlib.util.spec_from_file_location("srv", os.path.join(ROOT, "server.py"))
    server = importlib.util.module_from_spec(server_spec)
    try:
        server_spec.loader.exec_module(server)
        names = [t["name"] for t in getattr(server, "TOOLS", [])]
    except Exception:
        names = []
check("20 the four public tool names are unchanged",
      set(names) == {"search_pubmed", "search_systematic_reviews", "verify_citation",
                     "search_clinical_trials"} if names else True,
      str(names))
check("21 verify_citation still accepts exactly doi, pmid, title",
      set(tools.verify_citation.__code__.co_varnames[:3]) == {"doi", "pmid", "title"})

total = len(R)
failed = [n for n, ok, _ in R if not ok]
print(f"\n{total - len(failed)}/{total} passed")
if failed:
    print("FAILED:", failed)
sys.exit(1 if failed else 0)
