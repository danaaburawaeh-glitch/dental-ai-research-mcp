"""
connectors/pubmed/models.py

PubMed-specific constants and design-classification mapping (Phase 3/7 RCT disambiguation).
"""

# Publication-type tags, exactly as PubMed's own controlled vocabulary uses them.
# These are used to build ESearch [Publication Type] filters and to classify EFetch results —
# never inferred from free-text title/abstract matching.
PUBTYPE_SYSTEMATIC_REVIEW = "Systematic Review"
PUBTYPE_META_ANALYSIS = "Meta-Analysis"
PUBTYPE_RCT = "Randomized Controlled Trial"
PUBTYPE_CONTROLLED_TRIAL = "Controlled Clinical Trial"
PUBTYPE_REVIEW = "Review"
PUBTYPE_CASE_REPORTS = "Case Reports"
PUBTYPE_GUIDELINE = "Guideline"
PUBTYPE_PRACTICE_GUIDELINE = "Practice Guideline"

# MeSH terms used for designs without a clean Publication Type tag.
MESH_COHORT_STUDIES = "Cohort Studies"
MESH_OBSERVATIONAL_STUDY = "Observational Study"

# RCT disambiguation (Phase 3 hard requirement): this constant names the STUDY DESIGN sense
# of "RCT" explicitly and exclusively. It must never be conflated with the dental procedure
# "root canal treatment", which has no publication-type or MeSH representation of this kind —
# a dental-procedure sense of "RCT" appearing in a title/abstract does not populate this field;
# only PubMed's own PublicationType metadata does.
RCT_STUDY_DESIGN = PUBTYPE_RCT
RCT_DENTAL_PROCEDURE_DISAMBIGUATION_NOTE = (
    "RCT in dental literature may mean 'randomized controlled trial' (a study design) or "
    "'root canal treatment' (a clinical procedure). This module's RCT_STUDY_DESIGN constant "
    "refers only to the study-design sense, sourced only from PubMed's structured "
    "PublicationType field — never from free-text matching of the letters 'RCT'."
)


def build_publication_type_filter(study_type):
    """
    Map a caller-supplied study_type keyword to a PubMed [Publication Type] search term.
    Returns None if study_type is unrecognized (caller should then not apply a bogus filter
    rather than silently falling back to something incorrect).
    """
    mapping = {
        "systematic_review": (f'("{PUBTYPE_SYSTEMATIC_REVIEW}"[Publication Type]'
                              f' OR "{PUBTYPE_META_ANALYSIS}"[Publication Type])'),
        "meta_analysis": f'"{PUBTYPE_META_ANALYSIS}"[Publication Type]',
        "rct": f'"{PUBTYPE_RCT}"[Publication Type]',
        "controlled_trial": f'"{PUBTYPE_CONTROLLED_TRIAL}"[Publication Type]',
        "guideline": f'"{PUBTYPE_GUIDELINE}"[Publication Type] OR "{PUBTYPE_PRACTICE_GUIDELINE}"[Publication Type]',
        "cohort": f'"{MESH_COHORT_STUDIES}"[MeSH Terms]',
    }
    return mapping.get(study_type)
