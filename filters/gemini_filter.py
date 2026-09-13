"""Pass 2: Gemini fallback for jobs where regex found no years-of-experience mention."""

import os
import re
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

import config

load_dotenv()

MODEL = "gemini-3.1-flash-lite"

# Free-tier is rate-limited per minute (not just a daily cap) - space calls out and retry
# on 429 instead of giving up immediately, since the limit resets within seconds/minutes.
MIN_SECONDS_BETWEEN_CALLS = 4.5
MAX_RETRIES = 3
DEFAULT_RETRY_DELAY_SECONDS = 20

SYSTEM_INSTRUCTION = f"""You are helping a computer science graduate screen job postings for their first \
industry job. They have no formal industry job yet, but have built substantial personal and academic \
projects, and consider that enough to apply even to roles asking for around a year of experience - they'd \
rather see a borderline job and reject it themselves than have it silently hidden. Postings may be written \
in any language.

Given a job title and description, decide: should this candidate apply?

First, estimate years_estimate: the minimum years of experience the posting implies via an EXPLICIT number or \
range (0 if no explicit number is given; for a range, use the lower bound; ignore a lower number that only \
applies with an advanced degree the candidate doesn't have). When a requirement lists two alternatives joined \
by "or" (e.g. a plain degree OR a specific elite background/unit), satisfying either branch is enough - do \
not treat the more demanding branch as required just because it's mentioned.

Then decide fit:
- fit=true if years_estimate <= {config.MAX_YEARS_EXPERIENCE}. This includes postings with no explicit number \
at all, however strongly-worded the qualitative language is ("proven experience," "substantial experience," \
"mandatory," "extensive experience," "not your first job," a disclaimer that projects don't substitute for \
professional experience) - none of that alone should push years_estimate above \
{config.MAX_YEARS_EXPERIENCE}. The candidate would rather see a borderline job and reject it themselves than \
have it silently hidden - a title that's unambiguously senior/staff/expert-level has already been filtered \
out before you see it, so don't infer seniority from qualitative scope language alone.
- fit=false only when the posting states an explicit number or range above {config.MAX_YEARS_EXPERIENCE}.

If the response schema includes a reason field, give a one-sentence reason for your call, specific enough \
that the candidate can spot-check your reasoning later (e.g. "Explicitly requires 3+ years of Java \
experience" or "No experience requirement stated, and role sounds junior-friendly").
"""


class FitResult(BaseModel):
    """Minimal schema for production - no reason field, since generating a full sentence is the one part
    of this call with real added latency/output-token cost. years_estimate is a single extra token either
    way, so there's no reason to drop it too."""

    fit: bool
    years_estimate: int


class JobFitResult(BaseModel):
    """Verbose schema for the qa/ test tooling - adds the reason text for manual review."""

    fit: bool
    years_estimate: int
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


def classify(title: str, description: str, verbose: bool = False) -> JobFitResult:
    """Classify a job posting. verbose=True (qa/ tooling only) also asks for a reason string -
    the one part of this call with real added latency/output-token cost. Always returns the
    JobFitResult shape for a consistent caller-side interface; reason is "" when verbose=False.
    """
    global _quota_exceeded
    if _quota_exceeded:
        raise GeminiQuotaExceededError(
            "Gemini free-tier quota was already exhausted earlier this run"
        )

    client = _get_client()
    contents = f"Title: {title}\n\nDescription:\n{description}"
    schema = JobFitResult if verbose else FitResult

    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            parsed = response.parsed
            if verbose:
                return parsed
            return JobFitResult(fit=parsed.fit, years_estimate=parsed.years_estimate, reason="")
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
