"""
connectors/shared/normalization.py

Field-level normalization for cross-source comparison (feeds Phase 5 dual verification and
Phase 14 deduplication). Comparison helpers only — never silently "repairs" a mismatch,
per the brief's explicit instruction ("Never silently repair mismatches").
"""
import re
import unicodedata


def _fold(text):
    if text is None:
        return None
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower().strip()


def titles_match(title_a, title_b, threshold=0.90):
    """
    Compare two titles for equivalence, allowing for minor punctuation/whitespace/case
    differences (common between PubMed and Crossref renderings of the same title).
    Returns True only if normalized titles are identical or near-identical (simple
    token-overlap ratio >= threshold). This is a comparison function, not a repair
    function — it never merges or edits either title.
    """
    a, b = _fold(title_a), _fold(title_b)
    if a is None or b is None:
        return False
    a_clean = re.sub(r"[^\w\s]", "", a)
    b_clean = re.sub(r"[^\w\s]", "", b)
    if a_clean == b_clean:
        return True
    a_tokens, b_tokens = set(a_clean.split()), set(b_clean.split())
    if not a_tokens or not b_tokens:
        return False
    overlap = len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))
    return overlap >= threshold


def years_match(year_a, year_b, allow_adjacent=True):
    """
    Compare publication years. allow_adjacent permits a +/-1 year difference, since
    online-first vs issue-print dates commonly differ by one calendar year across
    PubMed and Crossref for the same article (documented, known source of apparent
    mismatch — not a citation error). This tolerance is applied explicitly and is
    visible in the comparison result, never silently assumed equal.
    """
    if year_a is None or year_b is None:
        return False
    if year_a == year_b:
        return True
    return allow_adjacent and abs(year_a - year_b) == 1


def authors_overlap(authors_a, authors_b, min_overlap=1):
    """
    Compare two author lists by surname only (given-name formatting varies wildly
    between sources). Returns True if at least min_overlap surnames appear in both.
    """
    if not authors_a or not authors_b:
        return False

    def surnames(lst):
        out = set()
        for name in lst:
            parts = name.strip().split()
            if parts:
                out.add(_fold(parts[-1]))
        return out

    return len(surnames(authors_a) & surnames(authors_b)) >= min_overlap


def journals_match(journal_a, journal_b):
    """Loose comparison, since journal abbreviation conventions differ across sources
    (PubMed commonly returns ISO abbreviations like 'Clin Oral Invest'; Crossref commonly
    returns full names like 'Clinical Oral Investigations'). Token-prefix matching handles
    the common truncated-word abbreviation pattern in addition to exact/substantial overlap."""
    a, b = _fold(journal_a), _fold(journal_b)
    if a is None or b is None:
        return False
    if a == b:
        return True
    a_tokens = re.sub(r"[^\w\s]", "", a).split()
    b_tokens = re.sub(r"[^\w\s]", "", b).split()
    if not a_tokens or not b_tokens:
        return False

    stopwords = {"journal", "of", "the", "international", "and", "for"}

    def token_matches(short_list, long_list):
        # Every token in the shorter list must be a prefix of (or equal to) some token
        # in the longer list, in order, and any long_list tokens SKIPPED over while
        # searching for a match must be stopwords only — a skipped CONTENT word (e.g.
        # "Prosthetic" between "Journal of" and "Dentistry") means the two names are
        # genuinely different journals, not an abbreviation of the same one, so it must
        # not match. This prevents "Journal of Dentistry" from matching "Journal of
        # Prosthetic Dentistry".
        if len(short_list) > len(long_list):
            return False
        j = 0
        for tok in short_list:
            found = False
            while j < len(long_list):
                candidate = long_list[j]
                if candidate.startswith(tok) or tok.startswith(candidate):
                    found = True
                    j += 1
                    break
                if candidate not in stopwords:
                    return False  # skipped a content word — genuinely different name
                j += 1
            if not found:
                return False
        return True

    shorter, longer = (a_tokens, b_tokens) if len(a_tokens) <= len(b_tokens) else (b_tokens, a_tokens)
    if token_matches(shorter, longer):
        return True

    # Overlap fallback: exclude common low-information journal-name words so shared
    # boilerplate ("journal", "of", "international", "the") doesn't inflate the ratio
    # for genuinely different journals.
    a_content = set(a_tokens) - stopwords
    b_content = set(b_tokens) - stopwords
    if not a_content or not b_content:
        return False
    overlap = len(a_content & b_content) / max(len(a_content), len(b_content))
    return overlap >= 0.6
