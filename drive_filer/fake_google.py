"""An in-memory stand-in for the Google Drive v3 and Sheets v4 REST APIs, for tests and offline demos.

It answers the same URLs with the same JSON shapes and the same error bodies
as the real APIs, for the subset this tool uses, and it enforces the rules
that trip people up in real life:
- a service account only sees files under a folder shared with it (404 otherwise);
- a service account has no storage quota, so it can create folders but not
  files (403 storageQuotaExceeded), which is why the log Sheet is made by a person;
- a file has exactly one parent (adding a second is 403 cannotAddParent);
- Google Docs/Sheets cannot be downloaded, only exported (403 fileNotDownloadable);
- appProperties are limited to 124 bytes per key plus value;
- search queries use Drive's q syntax, and an unknown clause is 400 invalid;
- results come in pages with nextPageToken;
- 429 / 403 rateLimitExceeded can be injected to test backoff.
Every request is recorded in .requests so tests can assert, for example,
that nothing was ever deleted.
"""
from __future__ import annotations

import hashlib
import itertools
import json as _json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote, urlparse

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SA_EMAIL = "filer-bot@demo-project.iam.gserviceaccount.com"
PERSON = "owner@example.com"
EXPORTABLE = {DOC: {"text/plain", "application/pdf"}, SHEET: {"text/csv", "application/pdf"}}


class FakeResponse:
    def __init__(self, status: int, body=None, content: bytes = b"", headers: Optional[dict] = None):
        self.status_code = status
        self._body = body
        self.content = content if body is None else _json.dumps(body).encode()
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        if self._body is None:
            return _json.loads(self.content)
        return self._body


def _error(status: int, reason: str, message: str, headers: Optional[dict] = None) -> FakeResponse:
    return FakeResponse(status, {"error": {"code": status, "message": message,
                                           "errors": [{"domain": "global", "reason": reason, "message": message}]}},
                        headers=headers)


@dataclass
class FakeFile:
    id: str
    name: str
    mimeType: str
    parents: list
    owner: str = PERSON
    content: bytes = b""
    description: str = ""
    appProperties: dict = field(default_factory=dict)
    trashed: bool = False
    modifiedTime: str = "2026-11-06T09:00:00.000Z"

    def resource(self) -> dict:
        d = {"id": self.id, "name": self.name, "mimeType": self.mimeType, "parents": list(self.parents),
             "modifiedTime": self.modifiedTime, "trashed": self.trashed,
             "webViewLink": f"https://drive.google.com/file/d/{self.id}/view"}
        if not self.mimeType.startswith("application/vnd.google-apps."):
            d["md5Checksum"] = hashlib.md5(self.content).hexdigest()
            d["size"] = str(len(self.content))
        if self.description:
            d["description"] = self.description
        if self.appProperties:
            d["appProperties"] = dict(self.appProperties)
        return d


class FakeGoogle:
    def __init__(self, page_size_cap: int = 1000):
        self.files: dict[str, FakeFile] = {}
        self.shared: set[str] = set()          # ids shared with the service account as Editor
        self.sheets: dict[str, dict] = {}      # spreadsheet id -> {"shared": bool, "tabs": {title: [rows]}}
        self.requests: list[tuple[str, str, dict]] = []
        self.change_log: list[str] = []        # file ids, in change order
        self._faults: list[dict] = []
        self._ids = itertools.count(1)
        self.page_size_cap = page_size_cap

    # ---- set-up helpers (what a person would do in the Drive web app) ----
    def _new_id(self, prefix: str = "f") -> str:
        return f"{prefix}{next(self._ids):04d}"

    def add_folder(self, name: str, parent: Optional[str] = None, shared: bool = False) -> str:
        fid = self._new_id("d")
        self.files[fid] = FakeFile(fid, name, FOLDER, [parent] if parent else ["root"])
        if shared:
            self.shared.add(fid)
        self.change_log.append(fid)
        return fid

    def add_file(self, name: str, parent: str, content: bytes, mime: str = "application/pdf") -> str:
        fid = self._new_id("f")
        self.files[fid] = FakeFile(fid, name, mime, [parent], content=content)
        self.change_log.append(fid)
        return fid

    def add_spreadsheet(self, shared: bool = True) -> str:
        sid = self._new_id("s")
        self.sheets[sid] = {"shared": shared, "tabs": {"Sheet1": []}}
        return sid

    def fail_next(self, count: int = 1, status: int = 429, reason: str = "rateLimitExceeded",
                  match: str = "", retry_after: Optional[str] = None) -> None:
        for _ in range(count):
            self._faults.append({"status": status, "reason": reason, "match": match, "retry_after": retry_after})

    def path_of(self, fid: str) -> str:
        parts = []
        f = self.files.get(fid)
        while f is not None and f.id not in self.shared:
            parts.append(f.name)
            f = self.files.get(f.parents[0]) if f.parents else None
        return "/".join(reversed(parts))

    def ancestors(self, fid: str) -> list[str]:
        out, f = [], self.files.get(fid)
        while f is not None:
            out.append(f.id)
            f = self.files.get(f.parents[0]) if f.parents else None
        return out

    def visible(self, fid: str) -> bool:
        return fid in self.files and any(a in self.shared for a in self.ancestors(fid))

    # ---- the HTTP surface -----------------------------------------------
    def request(self, method: str, url: str, params=None, json=None, data=None, headers=None, timeout=None):
        params = dict(params or {})
        self.requests.append((method, url, params))
        for i, fault in enumerate(self._faults):
            if fault["match"] in f"{method} {url}":
                self._faults.pop(i)
                hdr = {"Retry-After": fault["retry_after"]} if fault["retry_after"] else {}
                return _error(fault["status"], fault["reason"], "injected fault", hdr)
        u = urlparse(url)
        path = unquote(u.path)
        if u.netloc == "sheets.googleapis.com":
            return self._sheets(method, path, params, json)
        if not path.startswith("/drive/v3/"):
            return _error(404, "notFound", "unknown endpoint")
        rest = path[len("/drive/v3/"):]
        if method == "DELETE":
            return _error(403, "insufficientFilePermissions", "this tool must never delete")
        if rest == "files" and method == "GET":
            return self._list(params)
        if rest == "files" and method == "POST":
            return self._create(json or {})
        if rest == "changes/startPageToken":
            return FakeResponse(200, {"kind": "drive#startPageToken", "startPageToken": str(len(self.change_log) + 1)})
        if rest == "changes":
            return self._changes(params)
        m = re.fullmatch(r"files/([^/]+)(/export)?", rest)
        if not m:
            return _error(404, "notFound", "unknown endpoint")
        fid, export = m.group(1), bool(m.group(2))
        if not self.visible(fid):
            return _error(404, "notFound", f"File not found: {fid}.")
        f = self.files[fid]
        if export:
            allowed = EXPORTABLE.get(f.mimeType)
            if not allowed or params.get("mimeType") not in allowed:
                return _error(400, "badRequest", "The requested conversion is not supported.")
            return FakeResponse(200, content=f.content)
        if method == "GET":
            if params.get("alt") == "media":
                if f.mimeType.startswith("application/vnd.google-apps."):
                    return _error(403, "fileNotDownloadable", "Only files with binary content can be downloaded. "
                                                              "Use Export with Docs Editors files.")
                return FakeResponse(200, content=f.content)
            return FakeResponse(200, f.resource())
        if method == "PATCH":
            return self._patch(f, params, json or {})
        return _error(405, "methodNotAllowed", method)

    def _list(self, params: dict) -> FakeResponse:
        try:
            preds = [self._clause(c) for c in _split_and(params.get("q", ""))]
        except ValueError as e:
            return _error(400, "invalid", f"Invalid Value: {e}")
        hits = [f for f in self.files.values() if self.visible(f.id) and all(p(f) for p in preds)]
        hits.sort(key=lambda f: f.id)
        size = min(int(params.get("pageSize", 100)), self.page_size_cap)
        start = int(params.get("pageToken", 0))
        page = hits[start:start + size]
        body = {"kind": "drive#fileList", "files": [f.resource() for f in page]}
        if start + size < len(hits):
            body["nextPageToken"] = str(start + size)
        return FakeResponse(200, body)

    def _clause(self, c: str):
        c = c.strip()
        m = re.fullmatch(r"'((?:[^'\\]|\\.)*)' in parents", c)
        if m:
            pid = _unq(m.group(1))
            return lambda f: pid in f.parents
        if c == "trashed = false":
            return lambda f: not f.trashed
        m = re.fullmatch(r"(mimeType|name) (=|!=) '((?:[^'\\]|\\.)*)'", c)
        if m:
            attr, op, val = m.group(1), m.group(2), _unq(m.group(3))
            return (lambda f: getattr(f, attr) == val) if op == "=" else (lambda f: getattr(f, attr) != val)
        m = re.fullmatch(r"appProperties has \{ key='((?:[^'\\]|\\.)*)' and value='((?:[^'\\]|\\.)*)' \}", c)
        if m:
            k, v = _unq(m.group(1)), _unq(m.group(2))
            return lambda f: f.appProperties.get(k) == v
        m = re.fullmatch(r"fullText contains '((?:[^'\\]|\\.)*)'", c)
        if m:
            needle = _unq(m.group(1)).lower()

            def full_text(f):
                body = f.content.decode("utf-8", "ignore") if f.mimeType in (DOC, SHEET, "text/plain") else ""
                return needle in f"{f.name}\n{f.description}\n{body}".lower()
            return full_text
        raise ValueError(c)

    def _create(self, body: dict) -> FakeResponse:
        parents = body.get("parents") or ["root"]
        if not all(self.visible(p) for p in parents):
            return _error(404, "notFound", f"File not found: {parents[0]}.")
        if body.get("mimeType") != FOLDER:
            return _error(403, "storageQuotaExceeded", "Service Accounts do not have storage quota. Leverage shared "
                                                       "drives, or use OAuth delegation instead.")
        fid = self._new_id("d")
        self.files[fid] = FakeFile(fid, body.get("name", "Untitled"), FOLDER, parents, owner=SA_EMAIL)
        self.change_log.append(fid)
        return FakeResponse(200, self.files[fid].resource())

    def _patch(self, f: FakeFile, params: dict, body: dict) -> FakeResponse:
        add = [p for p in params.get("addParents", "").split(",") if p]
        remove = [p for p in params.get("removeParents", "").split(",") if p]
        for p in add:
            if not self.visible(p) or self.files[p].mimeType != FOLDER:
                return _error(404, "notFound", f"File not found: {p}.")
        if any(p not in f.parents for p in remove):
            return _error(400, "badRequest", "removeParents contains a folder that is not a parent")
        parents = [p for p in f.parents if p not in remove] + [p for p in add if p not in f.parents]
        if len(parents) > 1:
            return _error(403, "cannotAddParent", "Increasing the number of parents is not allowed. Use addParents "
                                                  "together with removeParents to move the file.")
        props = dict(f.appProperties)
        for k, v in (body.get("appProperties") or {}).items():
            if v is None:
                props.pop(k, None)
            else:
                if len(k.encode()) + len(str(v).encode()) > 124:
                    return _error(400, "invalidAppProperty", f"appProperty {k} is longer than 124 bytes")
                props[k] = str(v)
        if "name" in body and not str(body["name"]).strip():
            return _error(400, "badRequest", "name cannot be empty")
        f.parents = parents or f.parents
        f.name = body.get("name", f.name)
        f.description = body.get("description", f.description)
        f.appProperties = props
        self.change_log.append(f.id)
        return FakeResponse(200, f.resource())

    def _changes(self, params: dict) -> FakeResponse:
        start = int(params["pageToken"]) - 1
        size = int(params.get("pageSize", 100))
        ids = self.change_log[start:start + size]
        changes = []
        for fid in ids:
            if self.visible(fid):
                changes.append({"kind": "drive#change", "type": "file", "fileId": fid, "removed": False,
                                "file": self.files[fid].resource()})
        body = {"kind": "drive#changeList", "changes": changes}
        if start + size < len(self.change_log):
            body["nextPageToken"] = str(start + size + 1)
        else:
            body["newStartPageToken"] = str(len(self.change_log) + 1)
        return FakeResponse(200, body)

    # ---- Sheets ---------------------------------------------------------
    def _sheets(self, method: str, path: str, params: dict, body) -> FakeResponse:
        m = re.fullmatch(r"/v4/spreadsheets/([^/:]+)(?::batchUpdate|/values/(.+?)(:append)?)", path)
        if not m:
            return _error(404, "notFound", "unknown endpoint")
        sid, rng, append = m.group(1), m.group(2), bool(m.group(3))
        sheet = self.sheets.get(sid)
        if sheet is None or not sheet["shared"]:
            return FakeResponse(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                                "message": "The caller does not have permission"}})
        if rng is None:  # batchUpdate
            for req in (body or {}).get("requests", []):
                title = req["addSheet"]["properties"]["title"]
                if title in sheet["tabs"]:
                    return FakeResponse(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                        "message": f"A sheet with the name \"{title}\" already exists."}})
                sheet["tabs"][title] = []
            return FakeResponse(200, {"spreadsheetId": sid, "replies": [{}]})
        tm = re.fullmatch(r"'((?:[^']|'')+)'!([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?", rng)
        if not tm or tm.group(1) not in sheet["tabs"]:
            return FakeResponse(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                "message": f"Unable to parse range: {rng}"}})
        rows = sheet["tabs"][tm.group(1)]
        if append and method == "POST":
            rows.extend([list(map(_cell, r)) for r in body["values"]])
            return FakeResponse(200, {"spreadsheetId": sid, "updates": {"updatedRows": len(body["values"])}})
        if method == "PUT":
            start = int(tm.group(3) or 1) - 1
            for i, r in enumerate(body["values"]):
                while len(rows) <= start + i:
                    rows.append([])
                rows[start + i] = list(map(_cell, r))
            return FakeResponse(200, {"spreadsheetId": sid, "updatedRows": len(body["values"])})
        if method == "GET":
            first = int(tm.group(3) or 1) - 1
            last = int(tm.group(5)) if tm.group(5) else len(rows)
            values = [r for r in rows[first:last] if r]
            out = {"range": rng, "majorDimension": "ROWS"}
            if values:
                out["values"] = values
            return FakeResponse(200, out)
        return _error(405, "methodNotAllowed", method)


def _cell(v):
    return "" if v is None else str(v)


def _unq(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _split_and(q: str) -> list[str]:
    """Split a Drive query on ' and ' outside quotes and braces."""
    parts, depth, quote, buf, i = [], 0, False, "", 0
    while i < len(q):
        ch = q[i]
        if ch == "\\" and quote:
            buf += q[i:i + 2]
            i += 2
            continue
        if ch == "'":
            quote = not quote
        elif not quote and ch == "{":
            depth += 1
        elif not quote and ch == "}":
            depth -= 1
        if not quote and depth == 0 and q[i:i + 5] == " and ":
            parts.append(buf)
            buf, i = "", i + 5
            continue
        buf += ch
        i += 1
    if buf.strip():
        parts.append(buf)
    return parts
