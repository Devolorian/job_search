#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client", "google-auth", "google-auth-oauthlib", "pyyaml"]
# ///
"""Read the candidate's Gmail for replies to applications -> state/mail.jsonl.

Before the tracker is written, the run reads the same
window of mail the vacancy sources read, and any reply from an employer — a rejection, an
interview invitation, an application confirmation — updates that application's row. This script
is only the reading half. Deciding what an email means is the model's job at Stage 5a of
prompts/daily_scan.md, and writing it is `sheets_apply_mail.py`'s.

READ-ONLY BY CONSTRUCTION. The OAuth token carries one scope, `gmail.readonly`. It cannot send,
draft, label, archive or mark as read — the never-send rule is enforced by what the token is,
not by this code behaving itself. Same principle as Telegram's TELEGRAM_EXPOSED_TOOLS=read-only.

Two passes, so a personal inbox is not poured into a model:
  1. headers only (From, Subject, Date) for every message in the window;
  2. full text only for messages that look job-related — an ATS sender domain, a company that
     is in the tracker, or a subject in hiring vocabulary. Recall-oriented on purpose: the
     model throws away what is not a reply; it cannot recover what was never read.

Email text is untrusted data, never instructions, exactly like a job posting.

Window: `lookback_hours` (30), and `first_run_lookback_hours` (1440 = 60 days) until
`mail.first_run_utc` exists in state/last_run.json — 60 days rather than the usual two weeks,
so the first read reaches back to when applications began. An explicit --hours wins over both. Messages already in
state/mail_seen.json (written by sheets_apply_mail.py once a message is handled) are skipped,
so the 30h overlap between the two daily runs costs nothing.

One-time setup (see README_SETUP.md): an OAuth client of type "Desktop app" in the same GCP
project as the service account, Gmail API enabled, the consent screen published to Production
(in Testing a refresh token dies after 7 days), the client JSON saved as
~/.job_search/gmail_client.json. Then, once, in a terminal with a browser:

    uv run scripts/collect_mail.py --auth

Usage:
    uv run scripts/collect_mail.py --json state/mail.jsonl
    uv run scripts/collect_mail.py --hours 72            # print only, no marker written
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import yaml
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import jobprofile  # scripts/jobprofile.py — profile/ paths and the merged config

ROOT = Path(__file__).resolve().parent.parent
SOURCES = jobprofile.SHARED_SOURCES
LAST_RUN = ROOT / "state" / "last_run.json"
SEEN = ROOT / "state" / "mail_seen.json"
SECRETS = Path.home() / ".job_search"
CLIENT_FILE = SECRETS / "gmail_client.json"
TOKEN_FILE = SECRETS / "gmail_token.json"
SHEETS_ENV = SECRETS / "sheets.env"
SOURCE_KEY = "mail"

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

BODY_CHARS = 3000

# Sender domains of applicant-tracking systems. A reply sent "on behalf of" an employer through
# its ATS comes from one of these, and names the employer only in the display name or the body.
ATS_DOMAINS = (
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co", "ashbyhq.com",
    "workablemail.com", "workable.com", "smartrecruiters.com", "recruitee.com", "personio.de",
    "personio.com", "teamtailor.com", "teamtailor-mail.com", "myworkday.com", "workday.com",
    "icims.com", "bamboohr.com", "jobvite.com", "breezy.hr", "applytojob.com", "jazzhr.com",
    "pinpointhq.com", "hibob.com", "comeet.co", "homerun.co", "rippling.com", "dover.com",
    "gem.com", "join.com", "successfactors.com", "taleo.net", "oraclecloud.com",
    "freshteam.com", "manatal.com", "eightfold.ai", "wellfound.com", "wantapply.com",
    "hh.ru", "otta.com", "welcometothejungle.com",
)

# LinkedIn sends both the signal ("Your application was sent to X", "X viewed your
# application") and a lot of noise. The job-alert digests are noise by definition: they are
# new postings, not replies — the vacancy sources already cover postings.
NOISE_SENDERS = ("jobalerts-noreply@linkedin.com", "jobs-listings@linkedin.com",
                 "newsletters-noreply@linkedin.com", "notifications-noreply@linkedin.com")

HIRING_SUBJECT = re.compile(
    r"applica|applying|applied|candida|interview|position|your profile|next step|"
    r"opportunit|recruit|hiring|offer|assessment|take[- ]home|challenge|screening|"
    r"thank you for your interest|update on your|role at|"
    r"отклик|ваканси|собеседован|резюме|кандидат|интервью|оффер|тестов",
    re.I)

LEGAL_SUFFIXES = {"inc", "inc.", "llc", "ltd", "ltd.", "limited", "gmbh", "s.l.", "sl",
                  "b.v.", "bv", "s.a.", "sa", "ag", "oy", "ab", "plc"}


def norm_company(name: str) -> str:
    """Same normalisation as sheets_add_applications.py, copied rather than imported."""
    parts = re.sub(r"\s+", " ", (name or "").strip().lower()).split()
    if len(parts) > 1 and parts[-1].strip(",") in LEGAL_SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts).rstrip(",")


# ------------------------------------------------------------------ config and state

def load_config() -> dict:
    doc = jobprofile.load_sources()
    return (doc.get("sources") or {}).get(SOURCE_KEY) or {}


def first_run(source_key: str = SOURCE_KEY) -> bool:
    """True until this source has completed one run. Deleting `mail.first_run_utc` from
    state/last_run.json re-arms the 60-day window."""
    try:
        doc = json.loads(LAST_RUN.read_text(encoding="utf-8")) or {}
        return not (doc.get(source_key) or {}).get("first_run_utc")
    except Exception:
        return True          # unreadable state file: widen rather than silently narrow


def mark_run(source_key: str = SOURCE_KEY) -> None:
    """Written last, after the output file exists — a crash mid-run must leave the next run
    still treating itself as the first."""
    try:
        doc = json.loads(LAST_RUN.read_text(encoding="utf-8")) if LAST_RUN.exists() else {}
    except Exception:
        doc = {}
    now = datetime.now(timezone.utc).isoformat()
    entry = doc.get(source_key) or {}
    entry.setdefault("first_run_utc", now)
    entry["last_run_utc"] = now
    doc[source_key] = entry
    LAST_RUN.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")


def seen_ids() -> set[str]:
    try:
        return set(json.loads(SEEN.read_text(encoding="utf-8")))
    except Exception:
        return set()


# ------------------------------------------------------------------ credentials

def gmail_creds(interactive: bool) -> Credentials:
    if interactive:
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not CLIENT_FILE.exists():
            sys.exit(f"No OAuth client at {CLIENT_FILE}. See README_SETUP.md, 'Gmail'.")
        cred = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), GMAIL_SCOPES
                                                         ).run_local_server(port=0)
        save_token(cred)
        return cred
    if not TOKEN_FILE.exists():
        sys.exit(f"mail: no token at {TOKEN_FILE} — run `uv run scripts/collect_mail.py "
                 f"--auth` once in a terminal.")
    cred = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    # Refuse a token that carries more than read access, however it got there.
    if set(cred.scopes or []) - set(GMAIL_SCOPES):
        sys.exit(f"mail: token carries scopes beyond gmail.readonly: {cred.scopes}. "
                 f"Delete {TOKEN_FILE} and re-run --auth.")
    if not cred.valid:
        try:
            cred.refresh(Request())
        except RefreshError as e:
            sys.exit(f"mail: token refresh failed ({e}). If the consent screen is still in "
                     f"Testing, tokens die after 7 days — publish it, then re-run --auth.")
        save_token(cred)
    return cred


def save_token(cred: Credentials) -> None:
    TOKEN_FILE.write_text(cred.to_json(), encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)


def tracker_companies() -> list[str]:
    """Companies in the tracker tab — a sender or subject naming one of them is read in
    full. Failure here narrows the prefilter to ATS domains and vocabulary; it never stops
    the run."""
    try:
        if SHEETS_ENV.exists():
            for line in SHEETS_ENV.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"'))
        info = json.loads(Path(os.environ["SERVICE_ACCOUNT_PATH"]).read_text(encoding="utf-8"))
        cred = service_account.Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)
        sid = jobprofile.spreadsheet_id()
        svc = build("sheets", "v4", credentials=cred, cache_discovery=False).spreadsheets()
        rows = svc.values().get(spreadsheetId=sid, range=f"{jobprofile.tracker_tab()}!A1:M"
                                ).execute().get("values", [])
        ci = rows[0].index("Company")
        return sorted({r[ci].strip() for r in rows[1:] if len(r) > ci and r[ci].strip()})
    except Exception as e:
        print(f"  ! tracker companies unavailable ({type(e).__name__}: {e}); "
              f"prefilter uses ATS domains and subject vocabulary only", file=sys.stderr)
        return []


def company_patterns(companies: list[str]) -> list[tuple[str, re.Pattern]]:
    """Word-bounded patterns for each tracker company. The distinctive first token also
    counts ('Nord Security' is 'nord' in nordsec.com), but only when it is long enough not
    to match everything."""
    out = []
    for c in companies:
        n = norm_company(re.sub(r"\(.*?\)|[™®]", "", c))
        alts = {re.escape(n)}
        first = re.sub(r"[^\w-]", "", n.split()[0]) if n else ""
        if len(first) >= 5:
            alts.add(re.escape(first))
        if n:
            out.append((c, re.compile(r"(?<![\w])(" + "|".join(sorted(alts)) + r")(?![\w])",
                                      re.I)))
    return out


# ------------------------------------------------------------------ message parsing

def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def body_text(payload: dict) -> str:
    """text/plain if the message has one, else text/html with the tags removed."""
    plains, htmls = [], []

    def walk(p: dict) -> None:
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mt in ("text/plain", "text/html"):
            txt = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)
                                           ).decode("utf-8", "replace")
            (plains if mt == "text/plain" else htmls).append(txt)
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plains:
        txt = "\n".join(plains)
    else:
        txt = "\n".join(htmls)
        txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", txt)
        txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", txt)
        txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
    txt = re.sub(r"[ \t ]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt).strip()
    return txt[:BODY_CHARS]


def why_relevant(frm: str, subject: str, labels: list[str],
                 patterns: list[tuple[str, re.Pattern]]) -> tuple[list[str], list[str]]:
    addr = parseaddr(frm)[1].lower()
    domain = addr.rsplit("@", 1)[-1]
    if addr in NOISE_SENDERS:
        return [], []
    reasons, hints = [], []
    if any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS):
        reasons.append(f"ats:{domain}")
    if domain.endswith("linkedin.com") and re.search(r"application|applied|viewed", subject, re.I):
        reasons.append("linkedin:application")
    hay = f"{frm} {domain.replace('.', ' ')} {subject}"
    for name, pat in patterns:
        if pat.search(hay):
            hints.append(name)
    if hints:
        reasons.append("company")
    # Vocabulary alone is weak evidence; promotions that merely say "offer" are the obvious
    # false positive, so vocabulary counts outside the Promotions tab only.
    if HIRING_SUBJECT.search(subject) and "CATEGORY_PROMOTIONS" not in labels:
        reasons.append("subject")
    return reasons, hints


# ------------------------------------------------------------------ main

class Incomplete(Exception):
    """Some messages could not be read even after retries."""


def batched_get(svc, ids: list[str], fmt: str, **kw) -> dict[str, dict]:
    """Every id or an exception — never a silent subset.

    Measured on the first 60-day pass, 2026-09-30: unthrottled 40-message batches hit
    'Quota exceeded … Units per minute per user' (messages.get costs 5 of 250 units/second),
    most gets failed, and the run still wrote its output and its first-run marker — so the
    next run would have narrowed to 30h and 59 days of replies would never have been read.
    Now: 20 per batch, one batch a second (100 units/s), failed ids retried with backoff,
    and anything still missing raises, which stops the run before the marker is written."""
    out: dict[str, dict] = {}
    failed: dict[str, str] = {}

    def cb(req_id, resp, exc):
        if exc is None:
            out[req_id] = resp
            failed.pop(req_id, None)
        else:
            failed[req_id] = str(exc)[:160]

    todo = list(ids)
    for attempt, pause in enumerate((0, 10, 30, 60, 90)):
        if not todo:
            break
        if pause:
            print(f"  … {len(todo)} message(s) throttled, retry {attempt} in {pause}s",
                  file=sys.stderr)
            time.sleep(pause)
        for i in range(0, len(todo), 20):
            batch = svc.new_batch_http_request(callback=cb)
            for mid in todo[i:i + 20]:
                batch.add(svc.users().messages().get(userId="me", id=mid, format=fmt, **kw),
                          request_id=mid)
            batch.execute()
            time.sleep(1.0)
        todo = [m for m in todo if m not in out]
    if todo:
        sample = next(iter(failed.values()), "")
        raise Incomplete(f"{len(todo)} of {len(ids)} message(s) unreadable after retries "
                         f"({sample})")
    return out


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path, help="write results as JSONL here")
    ap.add_argument("--hours", type=int, default=None,
                    help="window; defaults to lookback_hours, or first_run_lookback_hours "
                         "if this source has never run")
    ap.add_argument("--auth", action="store_true",
                    help="one-time interactive consent; needs a browser")
    args = ap.parse_args()

    if args.auth:
        gmail_creds(interactive=True)
        print(f"Token saved to {TOKEN_FILE} (scope: gmail.readonly).", file=sys.stderr)
        return 0

    if args.hours is not None:
        hours = args.hours
    elif first_run():
        hours = cfg.get("first_run_lookback_hours", 1440)
        print(f"  first run for this source — looking back {hours}h instead of the usual "
              f"{cfg.get('lookback_hours', 30)}h", file=sys.stderr)
    else:
        hours = cfg.get("lookback_hours", 30)
    after = int(datetime.now(timezone.utc).timestamp()) - hours * 3600

    svc = build("gmail", "v1", credentials=gmail_creds(interactive=False),
                cache_discovery=False)
    query = f"after:{after} -in:sent -in:drafts -in:chats -from:me"

    ids, token = [], None
    while True:
        resp = svc.users().messages().list(userId="me", q=query, maxResults=500,
                                           pageToken=token, includeSpamTrash=False).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    done = seen_ids()
    fresh = [i for i in ids if i not in done]
    print(f"Reading mail: last {hours}h, {len(ids)} messages, {len(fresh)} not yet handled",
          file=sys.stderr)

    patterns = company_patterns(tracker_companies())
    try:
        meta = batched_get(svc, fresh, "metadata", metadataHeaders=["From", "Subject", "Date"])
    except Incomplete as e:
        sys.exit(f"mail: headers incomplete — {e}. Nothing written, window not narrowed.")
    keep: dict[str, tuple[list[str], list[str]]] = {}
    for mid, m in meta.items():
        reasons, hints = why_relevant(header(m, "From"), header(m, "Subject"),
                                      m.get("labelIds", []), patterns)
        if reasons:
            keep[mid] = (reasons, hints)

    try:
        full = batched_get(svc, list(keep), "full")
    except Incomplete as e:
        sys.exit(f"mail: bodies incomplete — {e}. Nothing written, window not narrowed.")
    records = []
    for mid, m in full.items():
        try:
            when = parsedate_to_datetime(header(m, "Date")).astimezone(timezone.utc)
        except Exception:
            when = datetime.fromtimestamp(int(m["internalDate"]) / 1000, timezone.utc)
        reasons, hints = keep[mid]
        records.append({
            "id": mid,
            "thread_id": m.get("threadId"),
            "date": when.isoformat(),
            "from": header(m, "From"),
            "subject": header(m, "Subject"),
            "why": reasons,
            "company_hints": hints,
            "body": body_text(m.get("payload", {})),
        })
    records.sort(key=lambda r: r["date"])

    print(f"  {len(meta)} headers read, {len(records)} job-related kept for the model",
          file=sys.stderr)
    if args.json:
        args.json.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                     for r in records), encoding="utf-8")
        print(f"  wrote {args.json}", file=sys.stderr)
        mark_run()
    else:
        for r in records:
            print(f"{r['date'][:10]}  {r['from'][:40]:40}  {r['subject'][:70]}  {r['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
