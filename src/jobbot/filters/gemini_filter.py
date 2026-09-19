"""Pass 2: Gemini fallback for jobs where regex found no years-of-experience mention."""

import os
import re
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from jobbot import config

load_dotenv()

MODEL = "gemini-3.1-flash-lite"

# Free-tier is rate-limited per minute (not just a daily cap) - space calls out and retry
# on 429 instead of giving up immediately, since the limit resets within seconds/minutes.
MIN_SECONDS_BETWEEN_CALLS = 4.5
MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 20

# Production prompt: promoted from qa/prompt_variants.py's quote_first_background_v3 after it scored
# best on real human labels (recall 1.000, precision 0.309 vs. 0.971 / 0.237 for the previous prompt -
# see the SCORES comment blocks there). It judges two independent leaf values - years_estimate and
# relevant_background - and classify() derives `fit` in Python; the model is never asked for `fit`.
SYSTEM_INSTRUCTION = """You are screening job postings for a CS graduate with no formal \
industry job yet, but substantial personal/academic projects - they'd rather see a borderline \
job and reject it themselves than have it silently hidden. Postings may be in any language.

Judge two independent things:

1. years_estimate: find and mentally quote the exact sentence(s) stating a required number of \
years of experience, if any. Qualitative language ("proven", "substantial", "mandatory", \
"extensive") without an explicit number does not count. Set years_estimate to the explicit \
number or the lower bound of a range (0 if none exists). Ignore a lower number that only applies \
with an advanced degree the candidate lacks. If two qualification paths are joined by "or", \
satisfying either is enough - don't take the more demanding path as the requirement. If the \
posting itself explicitly invites candidates with less experience than the stated number to \
apply anyway (e.g. "exceptional candidates with less experience are encouraged to apply", "this \
is a guideline, not a strict requirement") AND the stated number is modest (roughly 2-4 years, \
not a senior-level ask of 5+ years), treat that carve-out as satisfied and set years_estimate as \
if the lower bar applies. Never apply this carve-out to a senior-level requirement (5+ years) - \
generic "exceptional candidates considered" boilerplate on a senior posting doesn't override a \
genuinely senior ask.

2. relevant_background: true if this role's core discipline is software engineering, data \
science/engineering, machine learning/AI engineering, or product/technical program management. \
Data engineering means building data pipelines/infrastructure - NOT business intelligence \
(reporting, dashboarding, data-warehouse analytics), which counts as a different profession here \
even though the vocabulary overlaps. False only when the role is CLEARLY a different profession - \
sales, business/financial analysis, business intelligence, customer support, \
hardware/electrical/mechanical technician, administrative/operations, non-technical consulting - \
even if it shares a search keyword with a tech role. When genuinely ambiguous, or the role is \
technical-adjacent (e.g. a solutions/pre-sales role doing real engineering work), default to true \
- the same "reject it themselves rather than have it hidden" principle applies here too.

A senior/staff-level title has already been filtered out before you see this - don't infer \
seniority from scope/expertise language alone, and don't let seniority-adjacent language affect \
relevant_background either.

If the schema has a reason field, give one specific sentence a human could spot-check, covering \
whichever of the two judgments is most decisive.
"""


class FitResult(BaseModel):
    """Legacy years-only schema (judge_background=False), minimal form - no reason field. Only used to
    score the older prompt variants in qa/prompt_variants.py; production uses BackgroundJudgment."""

    fit: bool
    years_estimate: int


class JobFitResult(BaseModel):
    """Legacy years-only schema (judge_background=False), verbose form - adds the reason text."""

    fit: bool
    years_estimate: int
    reason: str


class BackgroundJudgment(BaseModel):
    """Production request schema (judge_background=True, the default): deliberately has NO `fit` field.
    qa/prompt_eval.py found the model's own `fit` field can silently disagree with its own
    years_estimate (e.g. years_estimate=1 with MAX_YEARS_EXPERIENCE=1 but fit=False anyway) - so `fit`
    is never asked of the model at all, only the independent leaf judgments (years_estimate,
    relevant_background), and classify() combines them deterministically in Python instead.
    No reason field here: generating a full sentence is the one part of the call with real added
    latency/output-token cost, so only the qa/ tooling asks for it (the Verbose* schemas)."""

    years_estimate: int
    relevant_background: bool


class VerboseBackgroundJudgment(BackgroundJudgment):
    reason: str


class FlexibleJudgment(BackgroundJudgment):
    """judge_flexibility=True: adds `experience_flexible` - does the posting ITSELF say its stated
    years number isn't strict ("flexible for exceptional builders", "years are indicative")? Asked as
    a plain fact because prose instructions to "then lower years_estimate" were measured NOT to work:
    the model quoted the flexible wording in its reason and still reported the full number. classify()
    applies the rule and its cap in Python instead (config.MAX_YEARS_WITH_FLEXIBLE_REQUIREMENT)."""

    experience_flexible: bool


class VerboseFlexibleJudgment(FlexibleJudgment):
    reason: str


class GeminiQuotaExceededError(RuntimeError):
    """Raised when the Gemini free-tier quota has been exhausted."""


_client = None
_quota_exceeded = False
_last_call_time = 0.0

_RETRY_DELAY_RE = re.compile(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'")


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set")
        _client = genai.Client(api_key=api_key)
    return _client


def _extract_retry_delay(e: errors.APIError) -> float:
    match = _RETRY_DELAY_RE.search(str(e.details))
    if match:
        return float(match.group(1))
    return DEFAULT_RETRY_DELAY_SECONDS


def _throttle() -> None:
    global _last_call_time
    elapsed = time.monotonic() - _last_call_time
    if elapsed < MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
    _last_call_time = time.monotonic()


def classify(
    title: str,
    description: str,
    verbose: bool = False,
    system_instruction: str | None = None,
    return_usage: bool = False,
    judge_background: bool = True,
    judge_flexibility: bool = False,
) -> "JobFitResult | tuple[JobFitResult, types.GenerateContentResponseUsageMetadata]":
    """Classify a job posting. verbose=True (qa/ tooling only) also asks for a reason string -
    the one part of this call with real added latency/output-token cost. Always returns the
    JobFitResult shape for a consistent caller-side interface; reason is "" when verbose=False.

    Production calls this with no extra arguments: the promoted SYSTEM_INSTRUCTION and the
    judge_background=True schema. system_instruction and return_usage exist for qa/prompt_eval.py
    (scoring alternative prompt text against the same call path production uses, and comparing token
    cost across variants).

    judge_background=True (the default) requests years_estimate + relevant_background (see
    BackgroundJudgment) and derives `fit` here as
    `years_estimate <= config.MAX_YEARS_EXPERIENCE and relevant_background`. Most low-precision false
    positives were jobs with a fine years requirement but the wrong professional background entirely
    (business/sales/support/hardware-technician roles slipping through broad search terms), which a
    years-only prompt was never asked to judge. judge_background=False is the legacy years-only
    schema (the model outputs `fit` itself) - kept only so qa/prompt_variants.py can still score the
    older prompt variants, which were written for that schema.

    judge_flexibility=True (implies judge_background) additionally requests `experience_flexible` and
    derives `fit` as `relevant_background and (years_estimate <= MAX_YEARS_EXPERIENCE or
    (experience_flexible and years_estimate <= MAX_YEARS_WITH_FLEXIBLE_REQUIREMENT))`, so the "less
    experience is welcome" exception - and its cap - are applied by code, not by the model.
    """
    global _quota_exceeded
    if _quota_exceeded:
        raise GeminiQuotaExceededError(
            "Gemini free-tier quota was already exhausted earlier this run"
        )

    client = _get_client()
    contents = f"Title: {title}\n\nDescription:\n{description}"
    if judge_flexibility:
        schema = VerboseFlexibleJudgment if verbose else FlexibleJudgment
    elif judge_background:
        schema = VerboseBackgroundJudgment if verbose else BackgroundJudgment
    else:
        schema = JobFitResult if verbose else FitResult
    instruction = system_instruction if system_instruction is not None else SYSTEM_INSTRUCTION

    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=instruction,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            parsed = response.parsed
            if judge_flexibility:
                years_ok = parsed.years_estimate <= config.MAX_YEARS_EXPERIENCE or (
                    parsed.experience_flexible
                    and parsed.years_estimate <= config.MAX_YEARS_WITH_FLEXIBLE_REQUIREMENT
                )
                fit = years_ok and parsed.relevant_background
            elif judge_background:
                fit = parsed.years_estimate <= config.MAX_YEARS_EXPERIENCE and parsed.relevant_background
            else:
                fit = parsed.fit
            reason = parsed.reason if verbose else ""
            result = JobFitResult(fit=fit, years_estimate=parsed.years_estimate, reason=reason)
            if return_usage:
                return result, response.usage_metadata
            return result
        except errors.APIError as e:
            # 429 = free-tier rate limit; 5xx = transient server-side issue ("high demand").
            # Both are worth retrying with backoff - anything else (400/403/404) is not transient.
            if e.code != 429 and e.code < 500:
                raise
            if attempt == MAX_RETRIES:
                if e.code == 429:
                    _quota_exceeded = True
                    raise GeminiQuotaExceededError(
                        "Gemini API quota exceeded (HTTP 429) after retries. "
                        "Remaining jobs will be marked fit=true (needs manual review) for this run."
                    ) from e
                raise
            delay = _extract_retry_delay(e) if e.code == 429 else DEFAULT_RETRY_DELAY_SECONDS
            print(f"[warning] Gemini {e.code} error, retrying in {delay:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(delay)
