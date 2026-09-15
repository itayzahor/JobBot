"""Pass 0: reject obviously-wrong titles (seniority/leadership) before spending regex or Gemini calls."""

import re

# Applied unconditionally, including to Product Manager-track titles.
SENIORITY_PATTERNS = [
    ("senior", re.compile(r"\bsenior\b", re.IGNORECASE)),
    # "\bsr\.?\b" would NOT match "Sr." - there's no word boundary between "." and a
    # following space/comma. Match the boundary right after "sr", then consume the dot.
    ("sr.", re.compile(r"\bsr\b\.?", re.IGNORECASE)),
    ("principal", re.compile(r"\bprincipal\b", re.IGNORECASE)),
    ("head of", re.compile(r"\bhead\s+of\b", re.IGNORECASE)),
    ("director", re.compile(r"\bdirector\b", re.IGNORECASE)),
    ("vp", re.compile(r"\bvp\b", re.IGNORECASE)),
    ("vice president", re.compile(r"\bvice\s+president\b", re.IGNORECASE)),
    ("chief", re.compile(r"\bchief\b", re.IGNORECASE)),
    ("team lead", re.compile(r"\bteam\s+lead(?:er)?\b", re.IGNORECASE)),
    # "Staff" sits above "Senior" on most IC ladders (Engineer -> Senior -> Staff -> Principal).
    ("staff", re.compile(r"\bstaff\b", re.IGNORECASE)),
    # [\s-]* (not ?) since real postings use both together, e.g. "Mid- Level".
    ("mid-level", re.compile(r"\bmid[\s-]*level\b", re.IGNORECASE)),
    # Hebrew equivalents. Substring match (no \b) since Hebrew gender/number suffixes
    # (e.g. בכיר -> בכירה, מנהל -> מנהלת) extend the root rather than needing a boundary.
    ("senior (he)", re.compile(r"בכיר")),
    # "ראש" (head) as a leading title word means "head of [team/project/dept]" - covers
    # "ראש צוות" (team lead) and more. Anchored at start + optional gender suffix (ראש/ת) so
    # it doesn't fire on "ראש" appearing mid-title in an unrelated sense.
    ("head of (he)", re.compile(r"^ראש[/.]?[תה]?(?=\s|$)")),
    # Company leveling suffixes (e.g. Google's "Software Engineer III"). Roman numeral as its
    # own token, followed by whitespace/comma/end - II and below is fine (per user: "II max").
    ("level 3+", re.compile(r"\b(III|IV|V|VI|VII|VIII|IX|X)\b(?=\s|,|$)")),
]

MANAGER_PATTERN = re.compile(r"\bmanager\b", re.IGNORECASE)
# "Product Manager" and "Program Manager" are individual-contributor roles despite the "manager"
# title - not people-management like "Engineering Manager"/"Manager, SW Eng". A plain substring
# search already covers "Technical Program Manager" etc. - whatever precedes "program"/"product"
# doesn't matter, no need to separately spell out qualified variants.
EXEMPT_MANAGER_PATTERN = re.compile(r"\b(?:product|program)[\s-]+manager\b", re.IGNORECASE)

# Hebrew "מנהל" (manager) - covers מנהל/מנהלת/מנהל.ת/מנהל/ת forms via substring match.
# Exempt "מנהל מוצר" (product manager) the same way the English check exempts "product manager".
MANAGER_HE_PATTERN = re.compile(r"מנהל")
PRODUCT_MANAGER_HE_PATTERN = re.compile(r"מנהל\S*\s*מוצר")

# Titles that are simply irrelevant professions slipping through broad search-term matches -
# not a seniority/leadership signal, just "wrong field entirely". Expand as more show up.
IRRELEVANT_ROLE_PATTERNS = [
    ("economist", re.compile(r"\beconomist\b", re.IGNORECASE)),
    ("economist (he)", re.compile(r"כלכל")),  # root shared by כלכלן/כלכלנית/כלכלן/ית
    ("student", re.compile(r"\bstudent\b", re.IGNORECASE)),
    ("student (he)", re.compile(r"סטודנט")),
]


def get_exclusion_reason(title: str) -> str | None:
    """Return why a title should be excluded, or None if it's fine."""
    if not title:
        return None

    for reason, pattern in SENIORITY_PATTERNS:
        if pattern.search(title):
            return reason

    if MANAGER_PATTERN.search(title) and not EXEMPT_MANAGER_PATTERN.search(title):
        return "manager"

    if MANAGER_HE_PATTERN.search(title) and not PRODUCT_MANAGER_HE_PATTERN.search(title):
        return "manager (he)"

    for reason, pattern in IRRELEVANT_ROLE_PATTERNS:
        if pattern.search(title):
            return reason

    return None
