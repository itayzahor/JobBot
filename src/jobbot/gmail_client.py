"""Read-only Gmail access for the Gmail agent: log in once, then fetch messages by date.

No AI here - this module only turns the mailbox into plain dicts. The model never sees the token
or calls Gmail itself; it only receives the text this module hands over.
"""

import base64
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from jobbot.paths import GMAIL_CREDENTIALS_PATH, GMAIL_TOKEN_PATH

# Read-only on purpose: even a leaked token can't send, delete or modify mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_HEADERS = ["From", "Subject", "Date"]


def get_service(interactive: bool = True):
    """Return an authenticated Gmail API client.

    Input: interactive - if no valid token exists, open a browser login (True) or raise (False;
    for the scheduled background run, which has no one to click through a login).
    Output: a googleapiclient Gmail service.
    """
    creds = None
    if GMAIL_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # Expired access token - refresh silently with the stored refresh token
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        flow = InstalledAppFlow.from_client_secrets_file(str(GMAIL_CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        raise RuntimeError("Gmail login missing or revoked - run scripts/gmail_preview.py to log in again")

    GMAIL_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(service, after: datetime, query: str = "") -> list[str]:
    """IDs of every message received after `after`, newest first.

    Input: after - timezone-aware datetime; query - extra Gmail search syntax (e.g. "-category:social").
    Output: list of message ids.
    """
    q = f"after:{int(after.timestamp())} {query}".strip()
    ids, page_token = [], None
    while True:
        resp = service.users().messages().list(userId="me", q=q, pageToken=page_token, maxResults=500).execute(num_retries=3)
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids


def get_message(service, message_id: str, with_body: bool = False) -> dict:
    """Fetch one message as a plain dict.

    Input: message_id; with_body - also download and decode the body (slower, bigger response).
    Output: {id, thread_id, received, sender, subject, labels, snippet[, body]}.
    """
    fmt = "full" if with_body else "metadata"
    msg = service.users().messages().get(userId="me", id=message_id, format=fmt, metadataHeaders=_HEADERS).execute(num_retries=3)
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", []) if h["name"] in _HEADERS}

    result = {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        # internalDate is when Gmail received it (ms epoch) - more reliable than the sender's Date header
        "received": datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc),
        "sender": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "labels": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
    }
    if with_body:
        result["body"] = _extract_body(msg["payload"])
    return result


def _extract_body(payload: dict) -> str:
    """Plain text of a message: the text/plain part if there is one, else the HTML part stripped."""
    plain, html = _find_parts(payload)
    if plain:
        return plain
    if html:
        return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    return ""


def _find_parts(payload: dict) -> tuple[str, str]:
    """Walk the MIME tree and return the first (text/plain, text/html) bodies found."""
    plain = html = ""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if data:
        text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        if mime == "text/plain":
            plain = text
        elif mime == "text/html":
            html = text
    for part in payload.get("parts", []):
        p, h = _find_parts(part)
        plain, html = plain or p, html or h
    return plain, html
