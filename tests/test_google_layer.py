"""The Drive/Sheets client against the fake, and the fake against the real API rules it claims to copy."""
from __future__ import annotations

import pytest

from drive_filer.drive import Drive, q_escape
from drive_filer.fake_google import DOC, SHEET, FakeGoogle, FakeResponse
from drive_filer.http import GoogleApiError, GoogleHttp, error_reason
from drive_filer.sheets import COLUMNS, SheetLog


@pytest.fixture
def g():
    return FakeGoogle()


def client(g, **kw):
    sleeps = []
    return Drive(GoogleHttp(g, sleep=sleeps.append, **kw)), sleeps


# --- backoff ---------------------------------------------------------------
def test_429_is_retried_with_growing_waits(g):
    root = g.add_folder("r", shared=True)
    d, sleeps = client(g)
    g.fail_next(3, 429)
    assert d.get(root).name == "r"
    assert len(sleeps) == 3 and sleeps[0] < sleeps[1] < sleeps[2]


def test_retry_after_header_is_honoured(g):
    root = g.add_folder("r", shared=True)
    d, sleeps = client(g)
    g.fail_next(1, 429, retry_after="7")
    d.get(root)
    assert sleeps == [7.0]


def test_403_rate_limit_and_5xx_are_retried(g):
    root = g.add_folder("r", shared=True)
    d, sleeps = client(g)
    g.fail_next(1, 403, "userRateLimitExceeded")
    g.fail_next(1, 503, "backendError")
    d.get(root)
    assert len(sleeps) == 2


def test_real_permission_errors_are_not_retried(g):
    d, sleeps = client(g)
    g.fail_next(1, 403, "insufficientFilePermissions")
    with pytest.raises(GoogleApiError) as e:
        d.get("x")
    assert e.value.reason == "insufficientFilePermissions" and sleeps == []


def test_gives_up_after_max_retries(g):
    d, sleeps = client(g, max_retries=2)
    g.fail_next(5, 429)
    with pytest.raises(GoogleApiError) as e:
        d.get("x")
    assert e.value.status == 429 and len(sleeps) == 2


def test_error_reason_reads_real_google_error_bodies():
    disabled = FakeResponse(403, {"error": {"code": 403, "message": "Google Drive API has not been used in project 1",
                                            "errors": [{"reason": "accessNotConfigured"}], "status": "PERMISSION_DENIED"}})
    assert error_reason(disabled)[0] == "accessNotConfigured"
    sheets = FakeResponse(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "no"}})
    assert error_reason(sheets)[0] == "PERMISSION_DENIED"
    assert error_reason(FakeResponse(500, content=b"<html>oops"))[0] == "unknown"


# --- Drive rules the fake enforces -------------------------------------------
def test_only_shared_folders_are_visible(g):
    shared = g.add_folder("shared", shared=True)
    other = g.add_folder("other")
    g.add_file("a.pdf", shared, b"1")
    g.add_file("b.pdf", other, b"2")
    d, _ = client(g)
    assert [f.name for f in d.search("trashed = false")] == ["shared", "a.pdf"]
    with pytest.raises(GoogleApiError) as e:
        d.get(other)
    assert e.value.status == 404


def test_listing_follows_page_tokens(g):
    root = g.add_folder("r", shared=True)
    for i in range(7):
        g.add_file(f"{i}.pdf", root, bytes([i]))
    g.page_size_cap = 3
    d, _ = client(g)
    assert len(list(d.children(root, folders=False))) == 7
    assert sum(1 for m, u, p in g.requests if u.endswith("/files")) == 3


def test_names_with_quotes_are_escaped_in_queries(g):
    root = g.add_folder("r", shared=True)
    g.add_folder("O'Brien's \\ files", root)
    d, _ = client(g)
    q = f"name = '{q_escape(chr(79) + chr(39) + 'Brien' + chr(39) + 's ' + chr(92) + ' files')}'"
    assert [f.name for f in d.search(q)] == ["O'Brien's \\ files"]


def test_unknown_query_clause_is_rejected(g):
    d, _ = client(g)
    with pytest.raises(GoogleApiError) as e:
        list(d.search("starred = true"))
    assert e.value.status == 400


def test_service_account_can_make_folders_but_not_files(g):
    root = g.add_folder("r", shared=True)
    d, _ = client(g)
    assert d.create_folder("Invoices", root).is_folder
    r = g.request("POST", "https://www.googleapis.com/drive/v3/files", json={"name": "log", "mimeType": SHEET,
                                                                             "parents": [root]})
    assert r.status_code == 403 and error_reason(r)[0] == "storageQuotaExceeded"


def test_move_needs_add_and_remove_parents(g):
    root = g.add_folder("r", shared=True)
    a = g.add_folder("a", root)
    b = g.add_folder("b", root)
    fid = g.add_file("x.pdf", a, b"x")
    d, _ = client(g)
    with pytest.raises(GoogleApiError) as e:
        d.update(fid, add_parents=[b])
    assert e.value.reason == "cannotAddParent"
    moved = d.update(fid, add_parents=[b], remove_parents=[a], name="y.pdf")
    assert moved.parents == [b] and moved.name == "y.pdf"


def test_cannot_move_outside_the_shared_tree(g):
    root = g.add_folder("r", shared=True)
    outside = g.add_folder("outside")
    fid = g.add_file("x.pdf", root, b"x")
    d, _ = client(g)
    with pytest.raises(GoogleApiError) as e:
        d.update(fid, add_parents=[outside], remove_parents=[root])
    assert e.value.status == 404


def test_app_properties_merge_delete_and_size_limit(g):
    root = g.add_folder("r", shared=True)
    fid = g.add_file("x.pdf", root, b"x")
    d, _ = client(g)
    d.update(fid, app_properties={"a": "1", "b": "2"})
    assert d.update(fid, app_properties={"a": None}).app_properties == {"b": "2"}
    with pytest.raises(GoogleApiError) as e:
        d.update(fid, app_properties={"k": "v" * 130})
    assert e.value.status == 400


def test_find_by_app_properties(g):
    root = g.add_folder("r", shared=True)
    fid = g.add_file("x.pdf", root, b"x")
    g.add_file("y.pdf", root, b"y")
    d, _ = client(g)
    d.update(fid, app_properties={"doc_type": "invoice", "period": "2026-10"})
    hits = list(d.find_by_app_properties({"doc_type": "invoice", "period": "2026-10"}))
    assert [f.id for f in hits] == [fid]


def test_binary_download_and_native_export(g):
    root = g.add_folder("r", shared=True)
    pdf = g.add_file("x.PDF", root, b"%PDF-1.4")
    doc = g.add_file("Notes", root, b"hello", DOC)
    sheet = g.add_file("Budget", root, b"a,b\n1,2", SHEET)
    d, _ = client(g)
    assert d.download(d.get(pdf)) == (b"%PDF-1.4", ".pdf")
    assert d.download(d.get(doc)) == (b"hello", ".txt")
    assert d.download(d.get(sheet)) == (b"a,b\n1,2", ".csv")
    r = g.request("GET", f"https://www.googleapis.com/drive/v3/files/{doc}", params={"alt": "media"})
    assert error_reason(r)[0] == "fileNotDownloadable"


def test_md5_is_reported_for_binary_files_only(g):
    root = g.add_folder("r", shared=True)
    d, _ = client(g)
    assert d.get(g.add_file("x.pdf", root, b"abc")).md5 == "900150983cd24fb0d6963f7d28e17f72"
    assert d.get(g.add_file("Doc", root, b"abc", DOC)).md5 is None


def test_changes_feed_pages_and_returns_new_token(g):
    root = g.add_folder("r", shared=True)
    d, _ = client(g)
    token = d.start_page_token()
    for i in range(150):
        g.add_file(f"{i}.pdf", root, bytes([i % 256]))
    changes, new_token = d.changes(token)
    assert len(changes) == 150 and int(new_token) > int(token)
    assert d.changes(new_token)[0] == []


def test_delete_is_refused_by_the_fake(g):
    r = g.request("DELETE", "https://www.googleapis.com/drive/v3/files/x")
    assert r.status_code == 403


# --- Sheets ---------------------------------------------------------------
def test_sheet_log_adds_tab_header_and_rows(g):
    sid = g.add_spreadsheet()
    log = SheetLog(GoogleHttp(g), sid)
    log.append([{"status": "filed", "file_id": "f1", "amount": 627.0}])
    log.append([{"status": "undone", "file_id": "f1"}])
    tab = g.sheets[sid]["tabs"]["Log"]
    assert tab[0] == COLUMNS
    assert [r["status"] for r in log.rows()] == ["filed", "undone"]
    assert log.rows()[0]["amount"] == "627.0"


def test_sheet_with_a_different_header_is_not_overwritten(g):
    sid = g.add_spreadsheet()
    g.sheets[sid]["tabs"]["Log"] = [["name", "email"]]
    with pytest.raises(RuntimeError):
        SheetLog(GoogleHttp(g), sid).append([{"status": "filed"}])
    assert g.sheets[sid]["tabs"]["Log"] == [["name", "email"]]


def test_sheet_not_shared_gives_permission_denied(g):
    sid = g.add_spreadsheet(shared=False)
    with pytest.raises(GoogleApiError) as e:
        SheetLog(GoogleHttp(g), sid).append([{"status": "filed"}])
    assert e.value.reason == "PERMISSION_DENIED"
