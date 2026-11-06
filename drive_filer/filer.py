"""The filing run: Inbox in, renamed and foldered files out, every change logged and reversible.

For each file in the Inbox:
  1. skip it if it was undone earlier or has failed three times;
  2. content hash (Drive's md5 for normal files, sha256 of the export for
     Google Docs/Sheets); a second copy of something already filed goes to
     Duplicates without a model call;
  3. download or export, turn into text, Claude reads it, code checks it;
  4. rules.py picks the folder and name (low confidence: Unsorted, name kept);
  5. one Drive PATCH renames, moves and labels it, wrapped in the undo log;
  6. a row is queued for the Sheet and written at the end (kept and retried
     if the Sheet is unreachable).
A failure on one file is logged and the file stays in the Inbox; the run
carries on with the next one.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .classify import LEVELS, CostCapReached, CostTracker, Reading
from .drive import EXPORTS, FOLDER, Drive, DriveFile, q_escape
from .http import GoogleApiError
from .loaders import SUPPORTED, UnsupportedFile, load_bytes
from .rules import Plan, Rules, plan, short_counterparty, with_suffix_number
from .store import Store, now_iso

TOOL = "drive-ai-document-filer"
MAX_ATTEMPTS = 3
MIN_TEXT = 20
PROP_KEYS = ["filer", "filer_status", "run_id", "doc_type", "doc_date", "period", "year", "counterparty",
             "amount", "currency", "reference", "confidence", "content_hash", "duplicate_of"]


@dataclass
class Outcome:
    file_id: str
    original_name: str
    status: str            # filed | unsorted | duplicate | error | skipped | planned
    new_name: str = ""
    folder: str = ""
    note: str = ""
    cost_usd: float = 0.0


@dataclass
class RunReport:
    run_id: str
    dry_run: bool
    outcomes: list[Outcome] = field(default_factory=list)
    cost_usd: float = 0.0
    stopped_by_cost_cap: bool = False
    left_in_inbox: int = 0
    retries: int = 0

    def counts(self) -> dict:
        out: dict = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out


def slug(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()[:limit]


def overall_confidence(reading: Reading) -> str:
    used = [f.confidence for f in reading.fields.values() if f.value is not None]
    return min(used, key=LEVELS.index) if used else "low"


def _fit(key: str, value: str) -> str:
    """Drive limits each appProperty to 124 bytes of key plus value."""
    room = 124 - len(key.encode())
    raw = str(value).encode()[:room]
    return raw.decode("utf-8", errors="ignore")


class Filer:
    def __init__(self, drive: Drive, store: Store, rules: Rules, root_id: str, inbox_id: str,
                 reader=None, sheet=None, dry_run: bool = False, cost_cap: Optional[float] = None,
                 log: Callable[[str], None] = print):
        self.drive = drive
        self.store = store
        self.rules = rules
        self.root_id = root_id
        self.inbox_id = inbox_id
        self.reader = reader
        self.sheet = sheet
        self.dry_run = dry_run
        self.costs = CostTracker(rules.cost_cap_usd if cost_cap is None else cost_cap,
                                 getattr(reader, "model", "claude-sonnet-5-5"))
        self.log = log
        self._folders: dict[tuple, str] = {}

    # --- folders ---------------------------------------------------------
    def folder_id(self, parts: list[str], create: bool = True) -> Optional[str]:
        parent = self.root_id
        for i in range(len(parts)):
            key = tuple(parts[: i + 1])
            if key not in self._folders:
                q = (f"'{q_escape(parent)}' in parents and name = '{q_escape(parts[i])}' "
                     f"and mimeType = '{FOLDER}' and trashed = false")
                found = next(iter(self.drive.search(q)), None)
                if found is None:
                    if not create:
                        return None
                    found = self.drive.create_folder(parts[i], parent)
                    self.log(f"  made folder {'/'.join(key)}")
                self._folders[key] = found.id
            parent = self._folders[key]
        return parent

    # --- a run -----------------------------------------------------------
    def scan(self, include_undone: bool = False) -> list[tuple[DriveFile, str]]:
        """What is in the Inbox and what would happen to it, without reading anything with the model."""
        out = []
        for f in self.drive.children(self.inbox_id, folders=False):
            out.append((f, self._skip_reason(f, include_undone) or "new"))
        return out

    def run(self, include_undone: bool = False, limit: Optional[int] = None) -> RunReport:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        report = RunReport(run_id, self.dry_run)
        if not self.dry_run:
            self.store.start_run(run_id, self.dry_run)
            self.recover_pending()
        files = list(self.drive.children(self.inbox_id, folders=False))
        for n, f in enumerate(files):
            if limit is not None and n >= limit:
                report.left_in_inbox += len(files) - n
                break
            try:
                report.outcomes.append(self.process(f, run_id, include_undone))
            except CostCapReached as e:
                self.log(f"  STOP: {e}; {len(files) - n} file(s) left in the Inbox for the next run")
                report.stopped_by_cost_cap = True
                report.left_in_inbox += len(files) - n
                break
        report.cost_usd = round(self.costs.spent, 6)
        report.retries = self.drive.http.retries
        if not self.dry_run:
            self.flush_sheet()
            self.store.finish_run(run_id, report.cost_usd, report.counts())
        return report

    def _skip_reason(self, f: DriveFile, include_undone: bool) -> str:
        row = self.store.file(f.id)
        if row is None:
            return ""
        if row["status"] == "undone" and not include_undone:
            return "undone earlier (use --include-undone to file it again)"
        if row["status"] == "error" and row["attempts"] >= MAX_ATTEMPTS:
            return f"failed {row['attempts']} times: {row['note']}"
        return ""

    def process(self, f: DriveFile, run_id: str, include_undone: bool = False) -> Outcome:
        skip = self._skip_reason(f, include_undone)
        if skip:
            self.log(f"- {f.name}: skipped, {skip}")
            return Outcome(f.id, f.name, "skipped", note=skip)
        try:
            return self._process(f, run_id)
        except CostCapReached:
            raise
        except (GoogleApiError, UnsupportedFile, ValueError, RuntimeError, OSError) as e:
            note = str(e)
            self.log(f"- {f.name}: ERROR {note} (left in the Inbox)")
            if not self.dry_run:
                attempts = self.store.bump_attempts(f.id, note, run_id)
                self.store.queue_row(self._row(run_id, "error", f, f.name, "Inbox", None, "",
                                               note=f"attempt {attempts}: {note}"))
            return Outcome(f.id, f.name, "error", note=note)

    def _process(self, f: DriveFile, run_id: str) -> Outcome:
        ext = self._ext(f)
        if ext not in SUPPORTED:
            return self._apply(f, run_id, Plan("unsorted", [self.rules.unsorted], f.name,
                                               [f"file type {ext or f.mime_type} is not read"]), None, None)
        data, read_ext = self.drive.download(f)
        content_hash = f"md5:{f.md5}" if f.md5 else "sha256:" + hashlib.sha256(data).hexdigest()
        original = self._original_of(content_hash, f.id)
        if original is not None:
            return self._duplicate(f, run_id, content_hash, original)
        doc = load_bytes(data, read_ext, f.name)
        if len("".join(doc.text.split())) < MIN_TEXT:
            return self._apply(f, run_id, Plan("unsorted", [self.rules.unsorted], f.name, ["no readable text"]),
                               None, content_hash)
        reading = self.reader.read(doc.text, f.name, self.costs)
        p = plan(reading, f.name, "" if f.is_google_native else ext, self.rules)
        return self._apply(f, run_id, p, reading, content_hash)

    @staticmethod
    def _ext(f: DriveFile) -> str:
        if f.mime_type in EXPORTS:
            return EXPORTS[f.mime_type][1]
        dot = f.name.rfind(".")
        return f.name[dot:].lower() if dot > 0 else ""

    def _original_of(self, content_hash: str, file_id: str) -> Optional[dict]:
        row = self.store.filed_with_hash(content_hash, file_id)
        if row is not None:
            return {"id": row["file_id"], "name": row["name"]}
        # also ask Drive, so duplicates are caught even with a fresh state file or another machine
        for other in self.drive.find_by_app_properties({"filer": TOOL, "content_hash": _fit("content_hash", content_hash)}):
            if other.id != file_id:
                return {"id": other.id, "name": other.name}
        return None

    def _duplicate(self, f: DriveFile, run_id: str, content_hash: str, original: dict) -> Outcome:
        p = Plan("duplicate", [self.rules.duplicates], f.name, [f"same content as {original['name']} ({original['id']})"])
        return self._apply(f, run_id, p, None, content_hash, duplicate_of=original["id"])

    # --- the one write ---------------------------------------------------
    def _apply(self, f: DriveFile, run_id: str, p: Plan, reading: Optional[Reading], content_hash: Optional[str],
               duplicate_of: str = "") -> Outcome:
        status = p.status
        folder_path = "/".join(p.folder)
        cost = reading.cost_usd if reading else 0.0
        note = "; ".join(p.reasons)
        if self.dry_run:
            self.log(f"- {f.name}: would be {status} -> {folder_path}/{p.new_name}" + (f"  ({note})" if note else ""))
            return Outcome(f.id, f.name, "planned", p.new_name, folder_path, f"{status}: {note}".strip(": "), cost)

        dest = self.folder_id(p.folder)
        taken = {c.name for c in self.drive.children(dest, folders=False) if c.id != f.id}
        new_name = with_suffix_number(p.new_name, taken)
        props = self._props(run_id, status, reading, content_hash, duplicate_of)
        description = self._description(status, reading, note, f.name)
        before = {"name": f.name, "parents": f.parents, "description": f.description,
                  "app_properties": {k: f.app_properties.get(k) for k in props}}
        after = {"name": new_name, "parents": [dest], "description": description, "app_properties": props}
        action = self.store.begin_action(run_id, f.id, before, after)
        try:
            updated = self.drive.update(f.id, name=new_name, description=description, app_properties=props,
                                        add_parents=[dest], remove_parents=[p for p in f.parents if p != dest])
        except Exception:
            self.store.finish_action(action, "failed")
            raise
        self.store.finish_action(action, "applied")
        self.store.set_file(f.id, content_hash=content_hash, status=status, name=new_name, folder=folder_path,
                            run_id=run_id, note=note)
        self.store.queue_row(self._row(run_id, status, f, new_name, folder_path, reading, content_hash,
                                       link=updated.link or f.link, note=note))
        self.log(f"- {f.name}: {status} -> {folder_path}/{new_name}" + (f"  ({note})" if note else ""))
        return Outcome(f.id, f.name, status, new_name, folder_path, note, cost)

    def _props(self, run_id, status, reading, content_hash, duplicate_of) -> dict:
        props = {"filer": TOOL, "filer_status": status, "run_id": run_id}
        if content_hash:
            props["content_hash"] = content_hash
        if duplicate_of:
            props["duplicate_of"] = duplicate_of
        if reading is not None:
            v = reading.value
            if v("doc_type"):
                props["doc_type"] = v("doc_type")
            if v("doc_date"):
                props["doc_date"] = str(v("doc_date"))
                props["period"] = str(v("doc_date"))[:7]
                props["year"] = str(v("doc_date"))[:4]
            if v("counterparty"):
                props["counterparty"] = slug(short_counterparty(v("counterparty")))
            if v("amount") is not None:
                props["amount"] = f"{float(v('amount')):.2f}"
            for k in ("currency", "reference"):
                if v(k):
                    props[k] = str(v(k))
            props["confidence"] = overall_confidence(reading)
        return {k: _fit(k, val) for k, val in props.items()}

    def _description(self, status: str, reading: Optional[Reading], note: str, original_name: str) -> str:
        lines = [f"{status.capitalize()} by {TOOL} on {now_iso()[:10]}. Original name: {original_name}"]
        if reading is not None:
            for name, fr in reading.fields.items():
                if fr.value is not None:
                    lines.append(f"{name}: {fr.value} ({fr.confidence})")
        if note:
            lines.append(f"Why: {note}")
        return "\n".join(lines)[:4000]

    def _row(self, run_id, status, f, new_name, folder, reading, content_hash, link="", note="") -> dict:
        row = {"logged_at": now_iso(), "run_id": run_id, "status": status, "file_id": f.id, "link": link or f.link,
               "original_name": f.name, "new_name": new_name, "folder": folder, "content_hash": content_hash or "",
               "note": note}
        if reading is not None:
            for k in ("doc_type", "doc_date", "counterparty", "currency", "reference"):
                row[k] = reading.value(k)
            if reading.value("amount") is not None:
                row["amount"] = f"{float(reading.value('amount')):.2f}"
            row["confidence"] = overall_confidence(reading)
            row["needs_review"] = "; ".join(f"{k}: {fr.confidence}" for k, fr in reading.fields.items()
                                            if fr.value is not None and fr.confidence == "low")
            row["model_cost_usd"] = round(reading.cost_usd, 6)
        return row

    def flush_sheet(self) -> int:
        queued = self.store.queued_rows()
        if not queued or self.sheet is None:
            return 0
        try:
            self.sheet.append([row for _, row in queued])
        except Exception as e:  # the Drive changes are done; keep the rows and try again next run
            self.log(f"  Sheet not updated ({e}); {len(queued)} row(s) kept for the next run")
            return 0
        self.store.clear_rows([i for i, _ in queued])
        return len(queued)

    # --- crash recovery and undo -----------------------------------------
    def recover_pending(self) -> None:
        """A 'pending' action means the process stopped around a PATCH. Look at Drive to see which side it is on."""
        for a in self.store.actions(state="pending"):
            try:
                cur = self.drive.get(a["file_id"])
            except GoogleApiError:
                continue
            done = cur.name == a["after"]["name"] and sorted(cur.parents) == sorted(a["after"]["parents"])
            self.store.finish_action(a["id"], "applied" if done else "failed")

    def undo(self, *, run_id: Optional[str] = None, file_id: Optional[str] = None, force: bool = False) -> list[str]:
        """Reverse applied actions, newest first. Folders the filer made stay (empty folders are harmless)."""
        if run_id is None and file_id is None:
            run_id = self.store.last_run_with_actions()
            if run_id is None:
                return ["nothing to undo"]
        messages = []
        undone_rows = []
        for a in self.store.actions(run_id=run_id, file_id=file_id, state="applied"):
            try:
                cur = self.drive.get(a["file_id"])
            except GoogleApiError as e:
                messages.append(f"{a['file_id']}: cannot read it ({e.reason}), not undone")
                continue
            moved_since = cur.name != a["after"]["name"] or sorted(cur.parents) != sorted(a["after"]["parents"])
            if moved_since and not force:
                messages.append(f"{cur.name}: changed by someone since it was filed, not undone (use --force)")
                continue
            before = a["before"]
            restore = {k: before["app_properties"].get(k) for k in a["after"]["app_properties"]}  # None removes the key
            if self.dry_run:
                messages.append(f"{cur.name}: would go back to {before['name']}")
                continue
            self.drive.update(cur.id, name=before["name"], description=before["description"], app_properties=restore,
                              add_parents=[p for p in before["parents"] if p not in cur.parents],
                              remove_parents=[p for p in cur.parents if p not in before["parents"]])
            self.store.mark_undone(a["id"])
            self.store.set_file(cur.id, status="undone", name=before["name"], folder="Inbox")
            undone_rows.append({"logged_at": now_iso(), "run_id": a["run_id"], "status": "undone", "file_id": cur.id,
                                "link": cur.link, "original_name": cur.name, "new_name": before["name"],
                                "folder": "(back where it was)", "note": "undo"})
            messages.append(f"{cur.name}: back to {before['name']}")
        for row in undone_rows:
            self.store.queue_row(row)
        if not self.dry_run:
            self.flush_sheet()
        return messages or ["nothing to undo"]

    # --- watching for new files ------------------------------------------
    def changes_in_inbox(self) -> bool:
        """True if anything new landed in the Inbox since the last check (Drive's changes feed)."""
        token = self.store.get("page_token")
        if token is None:
            self.store.put("page_token", self.drive.start_page_token())
            return True  # first time: do a full pass
        changes, new_token = self.drive.changes(token)
        self.store.put("page_token", new_token)
        return any(not c.get("removed") and self.inbox_id in (c.get("file") or {}).get("parents", [])
                   for c in changes)
