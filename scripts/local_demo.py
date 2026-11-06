"""A real run: real Claude reading the sample files, with the in-memory fake standing in for Google Drive.

Uploads every file in samples/inbox plus the Google Doc to a fake Drive Inbox,
files them, re-runs (nothing to do), searches, then undoes the run.
Writes what it printed to examples/local-run/. Needs ANTHROPIC_API_KEY; spend is a few cents.
  python scripts/local_demo.py
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from drive_filer.classify import ClaudeReader
from drive_filer.drive import Drive
from drive_filer.fake_google import DOC, FakeGoogle
from drive_filer.filer import Filer
from drive_filer.http import GoogleHttp
from drive_filer.rules import Rules
from drive_filer.search import find
from drive_filer.sheets import SheetLog
from drive_filer.store import Store

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "examples" / "local-run"
MIME = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def tree(g: FakeGoogle, folder: str, indent: str = "") -> list[str]:
    lines = []
    kids = sorted((f for f in g.files.values() if f.parents == [folder]), key=lambda f: (f.mimeType != "application/vnd.google-apps.folder", f.name))
    for f in kids:
        if f.mimeType == "application/vnd.google-apps.folder":
            lines.append(f"{indent}{f.name}/")
            lines += tree(g, f.id, indent + "    ")
        else:
            lines.append(f"{indent}{f.name}")
    return lines


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    g = FakeGoogle()
    root = g.add_folder("AI Filer", shared=True)
    inbox = g.add_folder("Inbox", root)
    sheet_id = g.add_spreadsheet()
    for p in sorted((ROOT / "samples" / "inbox").iterdir()):
        mime = MIME.get(p.suffix) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        g.add_file(p.name, inbox, p.read_bytes(), mime)
    g.add_file("Northwind agreement", inbox, (ROOT / "samples" / "google_docs" / "Northwind agreement.txt").read_bytes(), DOC)

    printed: list[str] = []

    def log(line: str) -> None:
        print(line)
        printed.append(line)

    http = GoogleHttp(g)
    drive = Drive(http)
    store = Store()
    filer = Filer(drive, store, Rules.load(), root, inbox, reader=ClaudeReader(), sheet=SheetLog(http, sheet_id), log=log)

    log("$ drive-filer scan")
    for f, status in filer.scan():
        log(f"{f.name:<40} {status}")
    log("\n$ drive-filer file")
    report = filer.run()
    log(f"\nRun {report.run_id}: " + ", ".join(f"{k} {v}" for k, v in sorted(report.counts().items())))
    log(f"Model spend ${report.cost_usd:.4f} for {filer.costs.calls} Claude calls")
    log("\nDrive folder after the run:\nAI Filer/")
    log("\n".join("    " + line for line in tree(g, root)))

    log("\n$ drive-filer file   (run again)")
    again = filer.run()
    log(f"Run {again.run_id}: {again.counts() or 'nothing in the Inbox'}")

    for question in ["invoices from Brightwater in October", "receipts in November", "invoices over 1000"]:
        q, hits = find(drive, question)
        log(f'\n$ drive-filer find "{question}"')
        for f in hits:
            status = f.app_properties.get("filer_status", "")
            log(f"  {f.name}" + (f"  [{status}: needs a look]" if status != "filed" else ""))
        log(f"{len(hits)} match(es)")

    rows = g.sheets[sheet_id]["tabs"]["Log"]
    (OUT / "sheet-log.csv").write_text("\n".join(",".join(json.dumps(c) for c in r) for r in rows) + "\n", encoding="utf-8")
    labels = {f.name: {"appProperties": f.appProperties, "description": f.description}
              for f in g.files.values() if f.appProperties}
    (OUT / "drive-labels.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")

    log("\n$ drive-filer undo")
    for line in filer.undo():
        log(line)
    log("\nDrive folder after undo:\nAI Filer/")
    log("\n".join("    " + line for line in tree(g, root)))
    deletes = [r for r in g.requests if r[0] == "DELETE"]
    log(f"\nDELETE requests sent to Drive in the whole session: {len(deletes)}")
    (OUT / "terminal.txt").write_text("\n".join(printed) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
