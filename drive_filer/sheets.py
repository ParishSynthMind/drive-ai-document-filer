"""The Google Sheet log: one row per action, appended, never edited or cleared.

Undoing a filing appends an "undone" row rather than deleting the old one, so
the Sheet stays a complete history of what the filer did.
"""
from __future__ import annotations

from urllib.parse import quote

from .http import GoogleApiError, GoogleHttp

API = "https://sheets.googleapis.com/v4/spreadsheets"
COLUMNS = [
    "logged_at", "run_id", "status", "file_id", "link", "original_name", "new_name", "folder",
    "doc_type", "doc_date", "counterparty", "amount", "currency", "reference",
    "confidence", "needs_review", "content_hash", "model_cost_usd", "note",
]


def _range(tab: str, cells: str) -> str:
    return quote(f"'{tab}'!{cells}", safe="")


class SheetLog:
    def __init__(self, http: GoogleHttp, spreadsheet_id: str, tab: str = "Log"):
        self.http = http
        self.id = spreadsheet_id
        self.tab = tab
        self._ready = False

    def ensure_ready(self) -> None:
        """Make sure the tab exists and has the header row (the Sheet itself must already exist)."""
        if self._ready:
            return
        try:
            r = self.http.request("GET", f"{API}/{self.id}/values/{_range(self.tab, 'A1:Z1')}")
        except GoogleApiError as e:
            if e.status != 400:  # 400 = the tab is missing; anything else is a real problem
                raise
            self.http.request("POST", f"{API}/{self.id}:batchUpdate",
                              json={"requests": [{"addSheet": {"properties": {"title": self.tab}}}]})
            r = None
        header = (r.json().get("values") or [[]])[0] if r is not None else []
        if not header:
            self.http.request("PUT", f"{API}/{self.id}/values/{_range(self.tab, 'A1')}",
                              params={"valueInputOption": "RAW"}, json={"values": [COLUMNS]})
        elif header[: len(COLUMNS)] != COLUMNS:
            raise RuntimeError(f"Sheet tab '{self.tab}' has a different header row; use an empty tab for the log")
        self._ready = True

    def append(self, rows: list[dict]) -> None:
        if not rows:
            return
        self.ensure_ready()
        values = [["" if row.get(c) is None else row.get(c) for c in COLUMNS] for row in rows]
        self.http.request("POST", f"{API}/{self.id}/values/{_range(self.tab, 'A1')}:append",
                          params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                          json={"values": values})

    def rows(self) -> list[dict]:
        self.ensure_ready()
        r = self.http.request("GET", f"{API}/{self.id}/values/{_range(self.tab, 'A:Z')}")
        values = r.json().get("values") or []
        return [dict(zip(COLUMNS, v + [""] * (len(COLUMNS) - len(v)))) for v in values[1:]]
