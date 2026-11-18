#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["telethon", "pyyaml", "google-api-python-client", "google-auth"]
# ///
"""The queue for /apply, and the tracker write after a confirmed submission (CLAUDE.md rule 1,
"Applying").

  list    Tracker rows with Status Prepared (or empty), no Applied on, Match score >=
          apply.min_score (search_profile.yaml, default 70), found in the last
          apply.max_age_days (default 21), whose folder is on this machine with a rendered CV. Writes state/apply_queue.json and prints the queue and what was
          left out and why: LinkedIn Easy Apply (a LinkedIn posting whose Apply leads to the
          employer's site is queued with its linkedin.com link; /apply clicks through), Telegram
          or other direct messages, email-only applications.
  mark    After the candidate confirmed the batch and a form was submitted: Applied on (only if
          empty), Stage reached / Status = Applied. The same
          tg_notify.mark_applied the vacancy chat uses.
  close   A posting that stopped taking applications before one was sent (archived, "no longer
          accepting", a 404 on the employer's form): Status = closed, from Prepared or empty
          only. Done by default, no question asked
          (candidate, 2026-11-17); funnel_report.py counts `closed` apart from `skipped`.
  resolve After /apply clicked a LinkedIn posting's offsite Apply: the employer's URL goes into
          source.json as employer_apply_url, so the next list links there directly.
  notify  One message in the candidate's private chat with their bot: how many are queued. For
          profile/hooks/post_run.sh after a scan; it never starts /apply.

Usage:
    uv run scripts/apply_queue.py list [--min-score 70] [--max-age-days 21]
    uv run scripts/apply_queue.py mark --folder vacancies/<dir> [--folder ...] [--date YYYY-MM-DD]
    uv run scripts/apply_queue.py close --folder vacancies/<dir> [--folder ...] --reason "..."
    uv run scripts/apply_queue.py resolve --folder vacancies/<dir> --url https://...
    uv run scripts/apply_queue.py notify
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobprofile  # noqa: E402  scripts/jobprofile.py — profile/ paths and the merged config
from sheets_add_applications import TAB, creds  # noqa: E402
from tg_notify import (BOT_ENV, a1, bot_api, load_posted, mark_applied, read_env,  # noqa: E402
                       save_posted)

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "state" / "apply_queue.json"

# A Telegram contact or an address to write to, in the posting text, when there is no form.
DIRECT = re.compile(r"(?<![\w.])@[A-Za-z][A-Za-z0-9_]{4,}\b|\bt\.me/|"
                    r"\b(write|message|dm|напиши|пиши|пишите|присылайте|отправляйте)\b[^.\n]{0,40}"
                    r"(@|telegram|телеграм|tg\b)", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def apply_cfg() -> dict:
    return jobprofile.load_search_profile().get("apply") or {}


def host(url: str) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def apply_link(folder: Path) -> tuple[str | None, str]:
    """The link /apply should open, or None and the reason it is left to the candidate."""
    d = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    links = [d.get(k) for k in ("employer_apply_url", "apply_url", "apply_link", "url")
             if d.get(k)]
    off_li = [u for u in links if "linkedin.com" not in host(u)]
    if not off_li:
        # Since 2026-11-15 a LinkedIn posting whose Apply leads to the employer's site is
        # applied to there; /apply clicks through in the browser. Easy Apply never.
        if d.get("li_apply") == "easy_apply":
            return None, "LinkedIn Easy Apply — by hand"
        return links[0] if links else None, "" if links else "no link"
    web = [u for u in off_li if host(u) not in ("t.me", "telegram.me")]
    job = (folder / "job.md").read_text(encoding="utf-8") if (folder / "job.md").exists() else ""
    if not web:
        return None, "Telegram — apply by direct message, by hand"
    if d.get("source", "").startswith("telegram") and DIRECT.search(job) and not d.get("apply_url"):
        return None, "Telegram post asks for a direct message — by hand"
    if re.search(r"\b(send|email|e-mail)\b[^.\n]{0,40}\b(cv|resume|résumé)\b", job, re.I) \
            and EMAIL.search(job) and not d.get("apply_url"):
        return None, "email-only application — by hand"
    return web[0], ""


def tracker_rows(svc, sid):
    rows = svc.values().get(spreadsheetId=sid, range=f"{TAB}!A1:M").execute().get("values", [])
    if not rows:
        return
    col = {n: i for i, n in enumerate(rows[0])}
    for i, r in enumerate(rows[1:], start=2):
        yield i, {n: (r[j].strip() if j < len(r) else "") for n, j in col.items()}


def cmd_list(args) -> int:
    cfg = apply_cfg()
    min_score = args.min_score if args.min_score is not None else int(cfg.get("min_score", 70))
    # A Prepared folder from weeks ago is mostly a closed posting by now.
    max_age = args.max_age_days if args.max_age_days is not None else int(cfg.get("max_age_days", 21))
    oldest = (date.today() - timedelta(days=max_age)).isoformat()
    sid = jobprofile.spreadsheet_id()
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False).spreadsheets()
    queue, left = [], []
    cv_name = jobprofile.cv_filename(jobprofile.load_master_cv())
    for rownum, r in tracker_rows(svc, sid):
        if r.get("Applied on") or r.get("Status", "").lower() not in ("", "prepared"):
            continue
        try:
            score = int(float(r.get("Match score") or 0))
        except ValueError:
            score = 0
        if score < min_score:
            continue
        rel = (r.get("Notes") or "").split()[:1]
        folder = ROOT / rel[0] if rel else None
        what = f"{r.get('Company')} — {r.get('Role')} ({score})"
        if folder and folder.name[:10] < oldest:
            continue
        if not folder or not (folder / "source.json").exists():
            left.append((what, "folder not on this machine"))
            continue
        cv = folder / cv_name
        if not cv.exists():
            left.append((what, "no rendered CV in the folder"))
            continue
        url, why = apply_link(folder)
        if not url:
            left.append((what, why))
            continue
        item = {"folder": str(folder.relative_to(ROOT)), "row": rownum,
                "company": r.get("Company"), "role": r.get("Role"), "score": score,
                "apply_url": url, "cv": str(cv.relative_to(ROOT))}
        if "linkedin.com" in host(url):
            # "offsite" from the collector, or "" for folders made before it recorded the
            # kind: /apply checks the button and stops on Easy Apply.
            item["via_linkedin"] = json.loads((folder / "source.json").read_text(
                encoding="utf-8")).get("li_apply", "unknown")
        for key, name in (("cover_letter_pdf", "cover_letter.pdf"),
                          ("cover_letter_txt", "cover_letter.txt"), ("why_join", "why_join.txt"),
                          ("form_answers_md", "form_answers.md")):
            if (folder / name).exists():
                item[key] = str((folder / name).relative_to(ROOT))
        queue.append(item)
    queue.sort(key=lambda q: -q["score"])
    QUEUE.write_text(json.dumps({"made_on": date.today().isoformat(), "min_score": min_score,
                                 "queue": queue}, ensure_ascii=False, indent=1) + "\n",
                     encoding="utf-8")
    print(f"Queue (score >= {min_score}, found since {oldest}): {len(queue)} -> "
          f"{QUEUE.relative_to(ROOT)}")
    for q in queue:
        print(f"  {q['score']:>3}  {q['company']} — {q['role']}\n       {q['apply_url']}")
    if left:
        print(f"\nLeft to the candidate: {len(left)}")
        for what, why in left:
            print(f"  - {what}: {why}")
    return 0


def cmd_mark(args) -> int:
    when = args.date or date.today().isoformat()
    applied = {}
    for f in args.folder:
        rel = str(Path(f)) if not Path(f).is_absolute() else str(Path(f).relative_to(ROOT))
        if not (ROOT / rel / "source.json").exists():
            sys.exit(f"not a vacancy folder: {rel}")
        applied[rel] = when
    marked = mark_applied(applied, "/apply",
                          "submitted through /apply (Claude in Chrome) after the candidate "
                          "confirmed the batch", args.dry_run)
    if args.dry_run:
        return 0
    # The vacancy chat must not log the same application again from a later reaction.
    state = load_posted()
    if state:
        for rel in marked:
            if rel in state["folders"]:
                state["folders"][rel]["applied_on"] = when
        save_posted(state)
    print(f"apply_queue mark: {len(marked)} of {len(applied)} marked Applied on {when}.")
    return 0 if len(marked) == len(applied) else 1


def cmd_close(args) -> int:
    """Status = closed on each folder's row (found by the folder path in Notes). Only a row at
    Prepared or empty changes: Applied, skipped and anything set by hand stay as they are."""
    folders = []
    for f in args.folder:
        rel = str(Path(f)) if not Path(f).is_absolute() else str(Path(f).relative_to(ROOT))
        if not (ROOT / rel / "source.json").exists():
            sys.exit(f"not a vacancy folder: {rel}")
        folders.append(rel)
    sid = jobprofile.spreadsheet_id()
    svc = build("sheets", "v4", credentials=creds(), cache_discovery=False).spreadsheets()
    header = (svc.values().get(spreadsheetId=sid, range=f"{TAB}!1:1")
              .execute().get("values", [[]]) or [[]])[0]
    col = {n: i for i, n in enumerate(header)}
    if any(n not in col for n in ("Company", "Role", "Status", "Notes")):
        sys.exit(f"{TAB} tab lacks Company/Role/Status/Notes — run setup_sheets.py first.")
    # The whole A:M block, never one column: see sheets_add_applications.py.
    rows = svc.values().get(spreadsheetId=sid, range=f"{TAB}!A2:M").execute().get("values", [])

    def cell(r, i):
        return r[i].strip() if len(r) > i else ""

    updates, closed = [], 0
    for rel in folders:
        i = next((i for i, r in enumerate(rows)
                  if cell(r, col["Notes"]).split()[:1] == [rel]), None)
        if i is None:
            print(f"  ! {rel}: no tracker row — nothing to close", file=sys.stderr)
            continue
        r = rows[i]
        company, role, status = cell(r, col["Company"]), cell(r, col["Role"]), cell(r, col["Status"])
        if status.lower() not in ("", "prepared"):
            print(f"  = {company} — {role}: Status {status!r} kept", file=sys.stderr)
            continue
        updates.append({"range": f"{TAB}!{a1(col['Status'])}{i + 2}", "values": [["closed"]]})
        change = f"{a1(col['Status'])} Status: {status!r} -> 'closed'"
        closed += 1
        print(f"  ~ {company} — {role}: {change} ({args.reason})", file=sys.stderr)
    if args.dry_run:
        print(f"--dry-run: {len(updates)} cell(s) not written.", file=sys.stderr)
        return 0
    if updates:
        svc.values().batchUpdate(spreadsheetId=sid, body={
            "valueInputOption": "USER_ENTERED", "data": updates}).execute()
    print(f"apply_queue close: {closed} of {len(folders)} set to closed.")
    return 0


def cmd_resolve(args) -> int:
    path = ROOT / args.folder / "source.json"
    if not path.exists():
        sys.exit(f"not a vacancy folder: {args.folder}")
    h = host(args.url)
    if not h or "linkedin.com" in h:
        sys.exit(f"not an employer URL: {args.url}")
    d = json.loads(path.read_text(encoding="utf-8"))
    d["employer_apply_url"] = args.url
    path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"apply_queue resolve: {args.folder} -> {args.url}")
    return 0


def cmd_notify(args) -> int:
    if not QUEUE.exists():
        print("apply_queue notify: no queue; run `list` first.", file=sys.stderr)
        return 0
    q = json.loads(QUEUE.read_text(encoding="utf-8"))["queue"]
    if not q:
        return 0
    env = read_env(BOT_ENV, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_CHAT_ID"))
    lines = [f"Готово к отклику: {len(q)}. Откройте Claude и запустите /apply.", ""]
    lines += [f"{x['score']} · {x['company']} — {x['role']}" for x in q[:15]]
    if len(q) > 15:
        lines.append(f"… и ещё {len(q) - 15}")
    bot_api(env["TELEGRAM_BOT_TOKEN"], "sendMessage", chat_id=env["TELEGRAM_BOT_CHAT_ID"],
            text="\n".join(lines))
    print(f"apply_queue notify: {len(q)} queued, message sent.", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--min-score", type=int, default=None)
    p.add_argument("--max-age-days", type=int, default=None)
    p = sub.add_parser("mark")
    p.add_argument("--folder", action="append", required=True)
    p.add_argument("--date")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("close")
    p.add_argument("--folder", action="append", required=True)
    p.add_argument("--reason", required=True, help="what the page said, e.g. 'archived on wantapply'")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("resolve")
    p.add_argument("--folder", required=True)
    p.add_argument("--url", required=True)
    sub.add_parser("notify")
    args = ap.parse_args()
    return {"list": cmd_list, "mark": cmd_mark, "close": cmd_close, "resolve": cmd_resolve,
            "notify": cmd_notify}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
