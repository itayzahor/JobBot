"""Gmail agent step 1 of 2: classify one email - what kind of application email it is, and which
company/role it's about. One structured Gemini call, no agent loop; matching it to a job in the DB is
a separate step.

Shares gemini_filter's client, throttle and retry settings on purpose: both modules spend the same
API key's rate limit, so one throttle has to cover both.
"""

import re
import time
from typing import Literal

from google.genai import errors, types
from pydantic import BaseModel

from jobbot.filters import gemini_filter as gf

EmailType = Literal[
    "confirmation", "rejection", "interview_invite", "home_assignment",
    "recruiter_outreach", "other_update", "unrelated",
]

# Rules come from labeling the user's real mailbox (60 days, 2026-11-01) - each line exists because
# a real email needed it.
SYSTEM_INSTRUCTION = """You sort a job seeker's incoming emails. Decide what kind of email this is \
and which application it is about. Emails are in English or Hebrew.

First copy into `evidence` the one sentence that decides the type (verbatim, original language; \
empty if the email is unrelated). Then choose `type`:

- confirmation: the application was received. Includes "thanks for applying", "we got your CV", \
"keep track of your application", "your application is already under review", and Indeed's \
"הגשת מועמדות דרך Indeed" / "application submitted" notices.
- rejection: the application is over. "Not moving forward", "proceed with other candidates", \
"position filled", "role closed / no longer open", "not relevant at this time" after a call - \
also when it comes after an interview or home task.
- interview_invite: the candidate is invited to talk - an interview, a phone/intro call, a calendar \
invite, or "you passed the task, when are you available?".
- home_assignment: a task, test, technical assessment or screening questionnaire to complete.
- recruiter_outreach: someone reaches out first about a role the candidate has NOT applied to - \
including an invitation to apply or a referral invite.
- other_update: about one specific application, but its status does not change - "sorry for the \
delay", "we received your task", a request for documents (transcript, GPA), "your application \
failed to send", "your application is incomplete / continue your draft", a match-score report.
- unrelated: everything else - job alerts and newsletters, "is this your next gig?" style \
marketing, talent-community promotions, account security and login emails, out-of-office \
auto-replies, "thanks for contacting us" replies from schools or businesses, and all \
personal, shopping, banking or university mail.

`company`: the company the candidate applied to. Never the hiring platform that sent the email \
(Comeet, Workday, Greenhouse, Lever, BambooHR, Ashby, Spark Hire, iCIMS, Manatal, adamtotal, \
Indeed, LinkedIn) - the real company is usually in the sender's display name, the sender's \
subdomain (no-reply@acme.comeet-notifications.com -> Acme) or the body. For a recruiting \
agency (a staffing / recruiting company), use the agency. Empty if the email doesn't say (Indeed notices name only \
the role).
`role`: the job title exactly as written in the email, empty if not named.
`job_reference`: a requisition / job ID if the email has one (JR2020001, R10001, ID: 10000001), \
else empty.
For unrelated emails leave company, role and job_reference empty."""

_MAX_BODY_CHARS = 4000

# Where a reply starts quoting the earlier thread (Gmail, Outlook, Hebrew Gmail). Without this the
# model judged a "role is equity-only" reply by the "happy to schedule a call" text it quoted.
_QUOTE_START = re.compile(
    r"\n\s*On [^\n]{0,150}?wrote:|\n_{10,}\s*\n\s*From:|-{3,}\s*Original Message|\n\s*בתאריך [^\n]{0,150}?כתב",
    re.IGNORECASE,
)


class EmailClassification(BaseModel):
    evidence: str
    type: EmailType
    company: str
    role: str
    job_reference: str


def strip_quoted(body: str) -> str:
    """The new part of a reply only - everything before the quoted earlier thread."""
    match = _QUOTE_START.search(body)
    return body[: match.start()] if match else body


def classify_email(
    sender: str, subject: str, body: str, return_usage: bool = False
) -> "EmailClassification | tuple[EmailClassification, types.GenerateContentResponseUsageMetadata]":
    """Classify one email.

    Input: sender header, subject, plain-text body (truncated here); return_usage - also return
    Gemini's token usage (qa/ scoring only).
    Output: EmailClassification, or (EmailClassification, usage) when return_usage.
    """
    contents = f"From: {sender}\nSubject: {subject}\n\n{strip_quoted(body)[:_MAX_BODY_CHARS]}"
    client = gf._get_client()

    for attempt in range(gf.MAX_RETRIES + 1):
        gf._throttle()
        try:
            response = client.models.generate_content(
                model=gf.MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=EmailClassification,
                ),
            )
            if return_usage:
                return response.parsed, response.usage_metadata
            return response.parsed
        except errors.APIError as e:
            # Same policy as gemini_filter: retry 429/5xx with backoff, fail fast on anything else
            if (e.code != 429 and e.code < 500) or attempt == gf.MAX_RETRIES:
                raise
            delay = gf._extract_retry_delay(e) if e.code == 429 else gf.DEFAULT_RETRY_DELAY_SECONDS
            print(f"[warning] Gemini {e.code} error, retrying in {delay:.0f}s (attempt {attempt + 1}/{gf.MAX_RETRIES})")
            time.sleep(delay)
