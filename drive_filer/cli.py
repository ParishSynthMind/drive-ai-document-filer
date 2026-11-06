"""drive-filer: check | scan | file | undo | report | find

Settings come from environment variables (see .env.example):
  DRIVE_ROOT_FOLDER_ID   the folder shared with the service account; everything is filed under it
  DRIVE_INBOX_FOLDER_ID  optional; default is the "Inbox" folder inside the root
  LOG_SHEET_ID           the Google Sheet for the log (made by a person and shared with the service account)
  FILER_STATE            optional; SQLite state file, default ~/.drive-filer/state.db
  ANTHROPIC_API_KEY      for Claude
plus one of the Google key settings described in auth.py.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .auth import authorized_session, load_key_info, service_account_email
from .classify import ClaudeReader
from .drive import Drive
from .filer import Filer, RunReport
from .http import GoogleApiError, GoogleHttp
from .rules import Rules
from .search import find
from .sheets import SheetLog
from .store import Store


def _need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Set {name} first (see .env.example).")
    return value


def build(args, need_reader: bool = False):
    info = load_key_info()
    http = GoogleHttp(authorized_session(info))
    drive = Drive(http)
    root = _need("DRIVE_ROOT_FOLDER_ID")
    inbox = os.environ.get("DRIVE_INBOX_FOLDER_ID", "").strip()
    store = Store(os.environ.get("FILER_STATE") or str(Path.home() / ".drive-filer" / "state.db"))
    rules = Rules.load(getattr(args, "config", None))
    sheet_id = os.environ.get("LOG_SHEET_ID", "").strip()
    sheet = SheetLog(http, sheet_id) if sheet_id else None
    reader = ClaudeReader() if need_reader else None
    filer = Filer(drive, store, rules, root, inbox or "", reader=reader, sheet=sheet,
                  dry_run=getattr(args, "dry_run", False), cost_cap=getattr(args, "cost_cap", None))
    if not inbox:
        filer.inbox_id = filer.folder_id(["Inbox"], create=not filer.dry_run) or ""
        if not filer.inbox_id:
            sys.exit("No 'Inbox' folder inside the root folder yet (a real run creates it).")
    return filer, info


def print_report(r: RunReport) -> None:
    counts = ", ".join(f"{k} {v}" for k, v in sorted(r.counts().items())) or "nothing in the Inbox"
    print(f"\nRun {r.run_id}{' (dry run, nothing changed)' if r.dry_run else ''}: {counts}")
    print(f"Model spend ${r.cost_usd:.4f}; Google retries {r.retries}"
          + (f"; stopped at the cost cap, {r.left_in_inbox} left in the Inbox" if r.stopped_by_cost_cap else ""))


def cmd_check(args) -> None:
    filer, info = build(args)
    print(f"Service account: {service_account_email(info)}")
    root = filer.drive.get(filer.root_id)
    print(f"Root folder: {root.name} ({root.id})")
    print(f"Inbox: {filer.inbox_id}, {sum(1 for _ in filer.drive.children(filer.inbox_id, folders=False))} file(s)")
    if filer.sheet:
        filer.sheet.ensure_ready()
        print(f"Log Sheet: OK, tab '{filer.sheet.tab}' ready")
    else:
        print("Log Sheet: not set (LOG_SHEET_ID)")


def cmd_scan(args) -> None:
    filer, _ = build(args)
    rows = filer.scan(include_undone=args.include_undone)
    for f, status in rows:
        print(f"{f.name:<50} {status}")
    print(f"{len(rows)} file(s) in the Inbox")


def cmd_file(args) -> None:
    filer, _ = build(args, need_reader=True)
    if not args.watch:
        print_report(filer.run(include_undone=args.include_undone, limit=args.limit))
        return
    print(f"Watching the Inbox every {args.interval}s (Ctrl-C to stop)")
    passes = 0
    while True:
        passes += 1
        if filer.changes_in_inbox() or passes % 30 == 1:  # changes feed, plus a full pass now and then
            print_report(filer.run(include_undone=args.include_undone, limit=args.limit))
        time.sleep(args.interval)


def cmd_undo(args) -> None:
    filer, _ = build(args)
    for line in filer.undo(run_id=args.run, file_id=args.file, force=args.force):
        print(line)


def cmd_report(args) -> None:
    filer, _ = build(args)
    if args.from_sheet:
        if not filer.sheet:
            sys.exit("LOG_SHEET_ID is not set")
        rows = filer.sheet.rows()
        counts: dict = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"Sheet log: {len(rows)} rows; " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return
    store = filer.store
    print("Files by status: " + (", ".join(f"{k} {v}" for k, v in sorted(store.counts().items())) or "none yet"))
    print(f"Model spend so far: ${store.total_cost():.4f}")
    for row in store.files_with_status("unsorted"):
        print(f"  Unsorted: {row['name']}  ({row['note']})")
    for row in store.files_with_status("error"):
        print(f"  Error ({row['attempts']}x): {row['file_id']}  {row['note']}")
    print("Recent runs:")
    for run in store.runs(5):
        print(f"  {run['run_id']}  {run['summary']}  ${run['cost_usd'] or 0:.4f}")


def cmd_find(args) -> None:
    filer, _ = build(args)
    q, results = find(filer.drive, args.query)
    shown = {k: v for k, v in vars(q).items() if v is not None}
    print(f"Looking for {shown or 'everything filed'}")
    for f in results:
        status = f.app_properties.get("filer_status", "")
        print(f"  {f.name}" + (f"  [{status}: needs a look]" if status != "filed" else "") + f"\n    {f.link}")
    print(f"{len(results)} match(es)")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="drive-filer", description="File Google Drive documents with Claude.")
    p.add_argument("--config", help="rules file (default config/filer.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test access to the folder and the Sheet")
    s = sub.add_parser("scan", help="list the Inbox and what would happen, no model calls")
    s.add_argument("--include-undone", action="store_true")
    f = sub.add_parser("file", help="file everything in the Inbox")
    f.add_argument("--dry-run", action="store_true", help="read and plan, change nothing")
    f.add_argument("--limit", type=int)
    f.add_argument("--cost-cap", type=float, help="US dollars of model spend for this run")
    f.add_argument("--include-undone", action="store_true", help="also file files that were undone before")
    f.add_argument("--watch", action="store_true", help="keep running and file new arrivals")
    f.add_argument("--interval", type=int, default=60)
    u = sub.add_parser("undo", help="put files back as they were (default: the last run)")
    u.add_argument("--run")
    u.add_argument("--file")
    u.add_argument("--force", action="store_true", help="undo even if someone moved the file since")
    u.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("report", help="what has been filed, what needs a look, what it cost")
    r.add_argument("--from-sheet", action="store_true")
    fd = sub.add_parser("find", help='e.g. find "invoices from Brightwater in October"')
    fd.add_argument("query")
    args = p.parse_args(argv)
    try:
        {"check": cmd_check, "scan": cmd_scan, "file": cmd_file, "undo": cmd_undo,
         "report": cmd_report, "find": cmd_find}[args.cmd](args)
    except GoogleApiError as e:
        hint = {"notFound": "is the folder shared with the service account's email?",
                "accessNotConfigured": "enable the Drive API and Sheets API in the Google Cloud project",
                "SERVICE_DISABLED": "enable the Drive API and Sheets API in the Google Cloud project",
                "PERMISSION_DENIED": "share the Sheet with the service account's email as Editor"}.get(e.reason, "")
        sys.exit(f"{e}" + (f"\nHint: {hint}" if hint else ""))


if __name__ == "__main__":
    main()
