"""find, the command line, the key loading and the file loaders."""
from __future__ import annotations

import base64
import json
from datetime import date

import pytest
from conftest import SAMPLES, invoice_text, raw

from drive_filer import auth, cli
from drive_filer.loaders import UnsupportedFile, load_bytes
from drive_filer.search import find, parse

TODAY = date(2026, 11, 6)


# --- search parsing ---------------------------------------------------------
def test_parse_the_example_question():
    q = parse("invoices from Brightwater in October", TODAY)
    assert (q.doc_type, q.counterparty, q.period) == ("invoice", "brightwater", "2026-10")


@pytest.mark.parametrize("text,period", [("receipts in December", "2025-12"), ("bills from Oakfield Sept 2026", "2026-09"),
                                         ("contracts last month", "2026-10"), ("invoices this month", "2026-11")])
def test_parse_months(text, period):
    assert parse(text, TODAY).period == period


def test_parse_year_amount_and_currency():
    q = parse("invoices over £500 in 2026 in GBP", TODAY)
    assert q.year == "2026" and q.min_amount == 500 and q.currency == "GBP" and q.period is None
    assert parse("receipts under 100 last year", TODAY).max_amount == 100
    assert parse("receipts under 100 last year", TODAY).year == "2025"


def test_parse_counterparty_stops_at_month():
    assert parse("anything from Kestrel Marine March", TODAY).counterparty == "kestrel marine"


def test_find_after_filing(env):
    env.put("a.txt", invoice_text())
    env.put("b.txt", invoice_text(cp="Oakfield Catering Co.", ref="OC-1", d="2026-10-21", total="453.60"))
    env.put("c.txt", invoice_text(d="2026-09-02", ref="BW-9", total="10.00"))
    env.filer({"a.txt": raw(),
               "b.txt": raw(doc_date="2026-10-21", counterparty="Oakfield Catering Co.", amount=453.6, reference="OC-1"),
               "c.txt": raw(doc_date="2026-09-02", reference="BW-9", amount=10.0)}).run()
    q, hits = find(env.drive, "invoices from Brightwater in October", TODAY)
    assert [f.name for f in hits] == ["2026-10-14 Brightwater Office Supplies invoice 627.00 GBP.txt"]
    _, all_bw = find(env.drive, "invoices from Brightwater", TODAY)
    assert len(all_bw) == 2
    _, big = find(env.drive, "invoices over 400", TODAY)
    assert sorted(f.app_properties["amount"] for f in big) == ["453.60", "627.00"]


def test_find_ignores_files_the_filer_did_not_label(env):
    env.put("loose.txt", b"Brightwater invoice October")
    assert find(env.drive, "invoices from Brightwater", TODAY)[1] == []


# --- credentials ------------------------------------------------------------
KEY = {"type": "service_account", "client_email": "bot@p.iam.gserviceaccount.com", "private_key": "not-a-real-key"}


def test_key_from_file_env_and_keychain(tmp_path):
    p = tmp_path / "k.json"
    p.write_text(json.dumps(KEY))
    assert auth.load_key_info({"GOOGLE_SERVICE_ACCOUNT_FILE": str(p)})["client_email"] == KEY["client_email"]
    assert auth.load_key_info({"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(KEY)})["type"] == "service_account"
    b64 = base64.b64encode(json.dumps(KEY).encode()).decode()
    got = auth.load_key_info({"GOOGLE_SA_KEYCHAIN_ITEM": "item"}, keychain_reader=lambda item: b64)
    assert got["client_email"] == KEY["client_email"]


def test_wrong_kind_of_key_and_no_key_are_refused():
    with pytest.raises(auth.MissingCredentials):
        auth.load_key_info({"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps({"type": "authorized_user"})})
    with pytest.raises(auth.MissingCredentials):
        auth.load_key_info({})


# --- CLI against the fake -----------------------------------------------------
@pytest.fixture
def cli_env(env, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_key_info", lambda: KEY)
    monkeypatch.setattr(cli, "authorized_session", lambda info: env.g)
    monkeypatch.setenv("DRIVE_ROOT_FOLDER_ID", env.root)
    monkeypatch.delenv("DRIVE_INBOX_FOLDER_ID", raising=False)
    monkeypatch.setenv("LOG_SHEET_ID", env.sheet_id)
    monkeypatch.setenv("FILER_STATE", str(tmp_path / "state.db"))

    class Reader:
        model = "claude-sonnet-5-5"

        def __init__(self):
            from conftest import ScriptedReader
            self.inner = ScriptedReader({"a.txt": raw()})

        def read(self, *a):
            return self.inner.read(*a)

    monkeypatch.setattr(cli, "ClaudeReader", Reader)
    return env


def test_cli_full_round(cli_env, capsys):
    fid = cli_env.put("a.txt", invoice_text())
    cli.main(["check"])
    out = capsys.readouterr().out
    assert "bot@p.iam.gserviceaccount.com" in out and "1 file(s)" in out and "Log Sheet: OK" in out
    cli.main(["scan"])
    assert "a.txt" in capsys.readouterr().out
    cli.main(["file", "--dry-run"])
    assert "dry run, nothing changed" in capsys.readouterr().out
    assert cli_env.where(fid) == "Inbox/a.txt"
    cli.main(["file"])
    assert "filed 1" in capsys.readouterr().out
    cli.main(["find", "invoices from Brightwater in October"])
    assert "1 match(es)" in capsys.readouterr().out
    cli.main(["report"])
    assert "filed 1" in capsys.readouterr().out
    cli.main(["report", "--from-sheet"])
    assert "1 rows" in capsys.readouterr().out
    cli.main(["undo"])
    assert "back to a.txt" in capsys.readouterr().out
    assert cli_env.where(fid) == "Inbox/a.txt"


def test_cli_explains_an_unshared_folder(cli_env, monkeypatch):
    monkeypatch.setenv("DRIVE_ROOT_FOLDER_ID", "not-shared")
    with pytest.raises(SystemExit) as e:
        cli.main(["check"])
    assert "shared with the service account" in str(e.value)


def test_cli_needs_the_root_folder(cli_env, monkeypatch):
    monkeypatch.delenv("DRIVE_ROOT_FOLDER_ID")
    with pytest.raises(SystemExit, match="DRIVE_ROOT_FOLDER_ID"):
        cli.main(["scan"])


# --- loaders on the real sample files ------------------------------------------
def test_pdf_text_layer():
    doc = load_bytes((SAMPLES / "inbox" / "brightwater_invoice.pdf").read_bytes(), ".pdf", "b.pdf")
    assert doc.method == "pdf-text" and "Brightwater" in doc.text


def test_scanned_pdf_uses_ocr():
    doc = load_bytes((SAMPLES / "inbox" / "kestrel_invoice_scanned.pdf").read_bytes(), ".pdf", "k.pdf")
    assert doc.method == "ocr" and "KESTREL" in doc.text.upper()


def test_photo_of_a_receipt_uses_ocr():
    doc = load_bytes((SAMPLES / "inbox" / "IMG_4471.png").read_bytes(), ".png", "IMG_4471.png")
    assert doc.method == "image-ocr" and "CITYLINE" in doc.text.upper() and "86.50" in doc.text


def test_word_and_excel():
    assert "Harborline" in load_bytes((SAMPLES / "inbox" / "harborline_print_invoice.docx").read_bytes(), ".docx").text
    assert "Oakfield" in load_bytes((SAMPLES / "inbox" / "oakfield_invoice.xlsx").read_bytes(), ".xlsx").text


def test_text_and_csv_exports():
    assert load_bytes(b"\xef\xbb\xbfhello", ".txt").text == "hello"
    assert load_bytes(b"a,b\n1,2\n", ".csv").text == "a | b\n1 | 2"


def test_unsupported_type():
    with pytest.raises(UnsupportedFile):
        load_bytes(b"PK", ".zip", "x.zip")
