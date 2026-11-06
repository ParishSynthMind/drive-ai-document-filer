from __future__ import annotations

from pathlib import Path

import pytest

from drive_filer.classify import CostTracker, Reading, check_reading
from drive_filer.drive import Drive
from drive_filer.fake_google import FakeGoogle
from drive_filer.filer import Filer
from drive_filer.http import GoogleHttp
from drive_filer.rules import Rules
from drive_filer.sheets import SheetLog
from drive_filer.store import Store

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def raw(doc_type="invoice", doc_date="2026-10-14", counterparty="Brightwater Office Supplies Ltd", amount=627.0,
        currency="GBP", reference="BW-2026-0412", conf="high", type_snippet=None, fields=None):
    """A Claude-shaped reply. Snippets are the values themselves, so the documents in tests must contain them."""
    values = {"doc_type": doc_type, "doc_date": doc_date, "counterparty": counterparty, "amount": amount,
              "currency": currency, "reference": reference}
    out = {}
    for k, v in values.items():
        snippet = "" if v is None else (f"{v:.2f}" if k == "amount" else str(v))
        if k == "doc_type" and v is not None:
            snippet = type_snippet or {"invoice": "TAX INVOICE", "receipt": "RECEIPT", "contract": "AGREEMENT",
                                       "bank_statement": "STATEMENT", "other": "NOTE"}[v]
        out[k] = {"value": v, "confidence": conf, "source_snippet": snippet}
    for k, v in (fields or {}).items():
        out[k] = {**out[k], **v}
    return out


def invoice_text(cp="Brightwater Office Supplies Ltd", ref="BW-2026-0412", d="2026-10-14", total="627.00", cur="GBP"):
    return f"TAX INVOICE\nFrom: {cp}\nInvoice number: {ref}\nDate: {d}\nTotal due: {cur} {total}\n".encode()


class ScriptedReader:
    """Stands in for ClaudeReader: replies are looked up by file name, then checked by the real checks."""

    model = "claude-sonnet-5-5"

    def __init__(self, replies: dict, tokens=(3000, 400)):
        self.replies = replies
        self.tokens = tokens
        self.calls = []

    def read(self, text: str, file_name: str, costs: CostTracker) -> Reading:
        costs.check(text)
        self.calls.append(file_name)
        cost = costs.add(*self.tokens)
        reply = self.replies[file_name]
        if isinstance(reply, Exception):
            raise reply
        return Reading(check_reading(reply, text), cost, *self.tokens)


class Env:
    def __init__(self):
        self.g = FakeGoogle()
        self.root = self.g.add_folder("AI Filer", shared=True)
        self.inbox = self.g.add_folder("Inbox", self.root)
        self.sheet_id = self.g.add_spreadsheet()
        self.sleeps = []
        self.http = GoogleHttp(self.g, sleep=self.sleeps.append)
        self.drive = Drive(self.http)
        self.store = Store()
        self.rules = Rules.load()
        self.sheet = SheetLog(self.http, self.sheet_id)
        self.logs = []

    def filer(self, replies: dict, **kw) -> Filer:
        self.reader = ScriptedReader(replies)
        return Filer(self.drive, self.store, self.rules, self.root, self.inbox, reader=self.reader,
                     sheet=self.sheet, log=self.logs.append, **kw)

    def put(self, name: str, content: bytes, mime: str = "text/plain") -> str:
        return self.g.add_file(name, self.inbox, content, mime)

    def where(self, fid: str) -> str:
        return self.g.path_of(fid)

    def sheet_rows(self):
        return self.g.sheets[self.sheet_id]["tabs"]["Log"]


@pytest.fixture
def env():
    return Env()
