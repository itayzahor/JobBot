"""Step 2: filter a job by title (regex), then required years of experience (Gemini).

Years-of-experience regex on the description was tried and dropped - "3-5 years" phrasing
comes in too many forms to chase reliably (en dashes, escaped Markdown, degree-conditional
alternates, ranges vs "nice to have" sections, etc.), and Gemini reads all of that correctly
without a growing pile of special-case patterns. Title-based regex (senior/manager/etc.) stays,
since that's plain word matching, not the fuzzy years-in-free-text problem.

If Gemini's free-tier quota ever becomes the binding constraint, `filters/regex_filter.py` is
still here to reintroduce as a cheap first pass.
"""

from jobbot.filters.gemini_filter import GeminiQuotaExceededError
from jobbot.filters.gemini_filter import classify as gemini_classify
from jobbot.filters.language_filter import is_allowed_language
from jobbot.filters.title_filter import get_exclusion_reason


def filter_job(title: str, description: str, verbose: bool = False) -> tuple[int | None, str, str]:
    """Return (min_years, decision, reason) for a job posting.

    verbose=True (qa/ tooling only) asks Gemini for a reason string too - production leaves this
    False since generating that sentence is the one part of the Gemini call with real added cost.
    Title/language-filtered jobs always get a reason regardless - it's just string formatting, not
    a Gemini call, so it costs nothing either way.
    """
    title_reason = get_exclusion_reason(title)
    if title_reason is not None:
        return None, "no", f"Title filtered: {title_reason}"

    if not is_allowed_language(description):
        return None, "no", "Description is not in Hebrew or English"

    try:
        result = gemini_classify(title, description, verbose=verbose)
    except GeminiQuotaExceededError as e:
        print(f"[warning] {e}")
        return None, "yes", "Gemini quota exceeded - not auto-screened, needs manual review"

    return result.years_estimate, "yes" if result.fit else "no", result.reason
