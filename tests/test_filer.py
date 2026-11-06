from __future__ import annotations

from conftest import invoice_text, raw

from drive_filer.fake_google import DOC
from drive_filer.filer import Filer
from drive_filer.store import Store


def test_invoice_is_renamed_and_filed_by_its_own_date(env):
    fid = env.put("scan from phone.txt", invoice_text())
    report = env.filer({"scan from phone.txt": raw()}).run()
    assert report.counts() == {"filed": 1}
    assert env.where(fid) == "Invoices/2026/10/2026-10-14 Brightwater Office Supplies invoice 627.00 GBP.txt"


def test_labels_are_written_to_drive_for_search(env):
    fid = env.put("a.txt", invoice_text())
    env.filer({"a.txt": raw()}).run()
    f = env.g.files[fid]
    assert f.appProperties["doc_type"] == "invoice"
    assert f.appProperties["period"] == "2026-10"
    assert f.appProperties["counterparty"] == "brightwater office supplies"
    assert f.appProperties["amount"] == "627.00"
    assert f.appProperties["filer_status"] == "filed"
    assert f.appProperties["content_hash"].startswith("md5:")
    assert "counterparty: Brightwater Office Supplies Ltd (high)" in f.description
    assert "Original name: a.txt" in f.description


def test_sheet_gets_header_and_one_row_per_file(env):
    env.put("a.txt", invoice_text())
    env.filer({"a.txt": raw()}).run()
    rows = env.sheet_rows()
    assert rows[0][:3] == ["logged_at", "run_id", "status"]
    assert rows[1][2] == "filed" and rows[1][5] == "a.txt"
    assert "drive.google.com" in rows[1][4]


def test_low_confidence_goes_to_unsorted_with_original_name(env):
    fid = env.put("kestrel.txt", invoice_text(cp="Kestrel Marine", d="2026-03-10"))
    reply = raw(counterparty="Kestrel Marine", doc_date="2026-03-10", fields={"doc_date": {"confidence": "low"}})
    report = env.filer({"kestrel.txt": reply}).run()
    assert report.outcomes[0].status == "unsorted"
    assert env.where(fid) == "Unsorted/kestrel.txt"
    assert "doc_date" in report.outcomes[0].note
    assert env.g.files[fid].appProperties["filer_status"] == "unsorted"


def test_value_not_in_document_is_not_trusted(env):
    # Claude says the supplier is "Acme Corp" but the document never says so: the check marks it down
    fid = env.put("a.txt", invoice_text())
    reply = raw(counterparty="Acme Corp")
    env.filer({"a.txt": reply}).run()
    assert env.where(fid).startswith("Unsorted/")


def test_duplicate_goes_to_duplicates_without_a_model_call(env):
    a = env.put("original.txt", invoice_text())
    b = env.put("copy.txt", invoice_text())
    filer = env.filer({"original.txt": raw(), "copy.txt": raw()})
    report = filer.run()
    assert sorted(o.status for o in report.outcomes) == ["duplicate", "filed"]
    assert env.reader.calls == ["original.txt"]
    assert env.where(b) == "Duplicates/copy.txt"
    assert env.g.files[b].appProperties["duplicate_of"] == a


def test_duplicate_caught_from_drive_labels_even_with_a_fresh_state_file(env):
    env.put("original.txt", invoice_text())
    env.filer({"original.txt": raw()}).run()
    b = env.put("copy.txt", invoice_text())
    env.store = Store()  # another machine, or the state file was lost
    env.filer({"copy.txt": raw()}).run()
    assert env.where(b) == "Duplicates/copy.txt"
    assert env.reader.calls == []


def test_google_doc_is_exported_and_keeps_no_extension(env):
    text = b"SERVICES AGREEMENT\nThis AGREEMENT is made on 2026-09-01 with Northwind Data Services Limited.\n"
    fid = env.g.add_file("Northwind agreement", env.inbox, text, DOC)
    reply = raw("contract", "2026-09-01", "Northwind Data Services Limited", None, None, None)
    env.filer({"Northwind agreement": reply}).run()
    assert env.where(fid) == "Contracts/2026/2026-09-01 Northwind Data Services contract"
    assert env.g.files[fid].appProperties["content_hash"].startswith("sha256:")
    assert any("/export" in url for _, url, _ in env.g.requests)


def test_unsupported_type_goes_to_unsorted_without_download_or_model(env):
    fid = env.put("backup.zip", b"PK\x03\x04", "application/zip")
    report = env.filer({}).run()
    assert report.outcomes[0].status == "unsorted"
    assert env.where(fid) == "Unsorted/backup.zip"
    assert not any(p.get("alt") == "media" for _, _, p in env.g.requests)


def test_empty_document_goes_to_unsorted(env):
    fid = env.put("blank.txt", b"   \n  ")
    env.filer({}).run()
    assert env.where(fid) == "Unsorted/blank.txt"


def test_dry_run_changes_nothing(env):
    fid = env.put("a.txt", invoice_text())
    filer = env.filer({"a.txt": raw()}, dry_run=True)
    report = filer.run()
    assert report.outcomes[0].status == "planned"
    assert report.outcomes[0].folder == "Invoices/2026/10"
    assert env.where(fid) == "Inbox/a.txt"
    assert not [m for m, _, _ in env.g.requests if m in ("POST", "PATCH", "PUT")]
    assert env.store.actions() == []


def test_second_run_finds_nothing_to_do(env):
    env.put("a.txt", invoice_text())
    filer = env.filer({"a.txt": raw()})
    filer.run()
    again = filer.run()
    assert again.outcomes == []
    assert env.reader.calls == ["a.txt"]


def test_a_failing_file_stays_in_inbox_and_the_run_carries_on(env):
    bad = env.put("bad.txt", invoice_text(ref="X-1"))
    good = env.put("good.txt", invoice_text(ref="X-2"))
    replies = {"bad.txt": RuntimeError("the model's reply was cut off"), "good.txt": raw(reference="X-2")}
    report = env.filer(replies).run()
    assert sorted(o.status for o in report.outcomes) == ["error", "filed"]
    assert env.where(bad) == "Inbox/bad.txt"
    assert env.where(good).startswith("Invoices/")
    assert env.store.file(bad)["attempts"] == 1
    assert any(r[2] == "error" for r in env.sheet_rows()[1:])


def test_a_file_that_keeps_failing_is_skipped_after_three_tries(env):
    env.put("bad.txt", invoice_text())
    filer = env.filer({"bad.txt": RuntimeError("boom")})
    for _ in range(3):
        filer.run()
    report = filer.run()
    assert report.outcomes[0].status == "skipped"
    assert len(env.reader.calls) == 3


def test_cost_cap_stops_the_run_and_leaves_the_rest(env):
    for i in range(5):
        env.put(f"inv{i}.txt", invoice_text(ref=f"R-{i}", total=f"{100 + i}.00"))
    replies = {f"inv{i}.txt": raw(reference=f"R-{i}", amount=100.0 + i) for i in range(5)}
    filer = env.filer(replies, cost_cap=0.05)
    report = filer.run()
    assert report.stopped_by_cost_cap
    assert report.cost_usd <= 0.05
    assert report.left_in_inbox == 5 - len(env.reader.calls)
    assert 0 < len(env.reader.calls) < 5
    assert sum(1 for f in env.g.files.values() if f.parents == [env.inbox]) == report.left_in_inbox


def test_same_name_in_folder_gets_a_number(env):
    a = env.put("a.txt", invoice_text(total="627.00"))
    b = env.put("b.txt", invoice_text(total="627.00", ref="BW-2026-0413"))
    env.filer({"a.txt": raw(), "b.txt": raw(reference="BW-2026-0413")}).run()
    names = sorted([env.g.files[a].name, env.g.files[b].name])
    assert names == ["2026-10-14 Brightwater Office Supplies invoice 627.00 GBP (2).txt",
                     "2026-10-14 Brightwater Office Supplies invoice 627.00 GBP.txt"]


def test_rate_limits_are_ridden_out(env):
    fid = env.put("a.txt", invoice_text())
    env.g.fail_next(2, 429, match="PATCH")
    env.g.fail_next(1, 403, "userRateLimitExceeded", match="GET")
    report = env.filer({"a.txt": raw()}).run()
    assert report.counts() == {"filed": 1}
    assert report.retries == 3
    assert env.where(fid).startswith("Invoices/")


def test_sheet_outage_keeps_rows_and_sends_them_next_run(env):
    env.put("a.txt", invoice_text())
    env.g.sheets[env.sheet_id]["shared"] = False
    filer = env.filer({"a.txt": raw()})
    filer.run()
    assert len(env.store.queued_rows()) == 1
    env.g.sheets[env.sheet_id]["shared"] = True
    filer.sheet._ready = False
    filer.run()
    assert env.store.queued_rows() == []
    assert len(env.sheet_rows()) == 2


def test_nothing_is_ever_deleted_or_trashed(env):
    env.put("a.txt", invoice_text())
    env.put("b.txt", invoice_text())
    env.put("z.zip", b"x", "application/zip")
    filer = env.filer({"a.txt": raw(), "b.txt": raw()})
    filer.run()
    filer.undo()
    assert not [m for m, _, _ in env.g.requests if m == "DELETE"]
    assert not any(f.trashed for f in env.g.files.values())
    assert not hasattr(filer.drive, "delete")


def test_undo_puts_everything_back(env):
    fid = env.put("phone scan.txt", invoice_text())
    filer = env.filer({"phone scan.txt": raw()})
    filer.run()
    messages = filer.undo()
    f = env.g.files[fid]
    assert env.where(fid) == "Inbox/phone scan.txt"
    assert f.appProperties == {} and f.description == ""
    assert "back to phone scan.txt" in messages[0]
    assert env.sheet_rows()[-1][2] == "undone"


def test_undo_one_file_only(env):
    a = env.put("a.txt", invoice_text(ref="A-1"))
    b = env.put("b.txt", invoice_text(ref="B-1", total="10.00"))
    filer = env.filer({"a.txt": raw(reference="A-1"), "b.txt": raw(reference="B-1", amount=10.0)})
    filer.run()
    filer.undo(file_id=a)
    assert env.where(a) == "Inbox/a.txt"
    assert env.where(b).startswith("Invoices/")


def test_undo_leaves_files_someone_moved_since_unless_forced(env):
    fid = env.put("a.txt", invoice_text())
    filer = env.filer({"a.txt": raw()})
    filer.run()
    env.g.files[fid].name = "renamed by a person.txt"
    assert "not undone" in filer.undo()[0]
    assert env.g.files[fid].name == "renamed by a person.txt"
    filer.undo(force=True)
    assert env.where(fid) == "Inbox/a.txt"


def test_undone_files_are_not_refiled_unless_asked(env):
    fid = env.put("a.txt", invoice_text())
    filer = env.filer({"a.txt": raw()})
    filer.run()
    filer.undo()
    assert filer.run().outcomes[0].status == "skipped"
    assert filer.run(include_undone=True).outcomes[0].status == "filed"
    assert env.where(fid).startswith("Invoices/")


def test_undo_dry_run_changes_nothing(env):
    fid = env.put("a.txt", invoice_text())
    env.filer({"a.txt": raw()}).run()
    filer = env.filer({}, dry_run=True)
    assert "would go back" in filer.undo()[0]
    assert env.where(fid).startswith("Invoices/")


def test_crash_between_log_and_patch_is_recovered(env):
    fid = env.put("a.txt", invoice_text())
    filer = env.filer({"a.txt": raw()})
    filer.run()
    action = env.store.actions()[0]
    env.store.finish_action(action["id"], "pending")  # as if the process died before marking it applied
    filer.recover_pending()
    assert env.store.actions()[0]["state"] == "applied"
    filer.undo()
    assert env.where(fid) == "Inbox/a.txt"


def test_folders_are_made_once_and_reused(env):
    for i in range(3):
        env.put(f"i{i}.txt", invoice_text(ref=f"R{i}", total=f"{i + 1}.00"))
    env.filer({f"i{i}.txt": raw(reference=f"R{i}", amount=float(i + 1)) for i in range(3)}).run()
    folders = [f for f in env.g.files.values() if f.name in ("Invoices", "2026", "10")]
    assert len(folders) == 3


def test_existing_folders_made_by_a_person_are_used(env):
    inv = env.g.add_folder("Invoices", env.root)
    y = env.g.add_folder("2026", inv)
    m = env.g.add_folder("10", y)
    fid = env.put("a.txt", invoice_text())
    env.filer({"a.txt": raw()}).run()
    assert env.g.files[fid].parents == [m]


def test_changes_feed_notices_new_inbox_files(env):
    filer = env.filer({})
    assert filer.changes_in_inbox() is True    # first call: full pass
    assert filer.changes_in_inbox() is False   # nothing new
    env.put("new.txt", invoice_text())
    assert filer.changes_in_inbox() is True


def test_long_values_are_trimmed_to_drive_property_limit(env):
    long_ref = "REF-" + "9" * 200
    fid = env.put("a.txt", invoice_text(ref=long_ref))
    env.filer({"a.txt": raw(reference=long_ref)}).run()
    for k, v in env.g.files[fid].appProperties.items():
        assert len(k.encode()) + len(v.encode()) <= 124


def test_receipt_and_statement_and_other_folders(env):
    r = env.put("r.txt", b"RECEIPT\nCityline Taxis\n2026-11-03\nAED 86.50\nCT-554120")
    s = env.put("s.txt", b"STATEMENT\nHarbour Bank\n2026-10-31\n")
    o = env.put("o.txt", b"NOTE\nMeeting notes 2026-08-12 about the office move\n")
    replies = {
        "r.txt": raw("receipt", "2026-11-03", "Cityline Taxis", 86.5, "AED", "CT-554120"),
        "s.txt": raw("bank_statement", "2026-10-31", "Harbour Bank", None, None, None),
        "o.txt": raw("other", "2026-08-12", None, None, None, None),
    }
    env.filer(replies).run()
    assert env.where(r) == "Receipts/2026/11/2026-11-03 Cityline Taxis receipt 86.50 AED.txt"
    assert env.where(s) == "Bank Statements/2026/2026-10-31 Harbour Bank bank statement.txt"
    assert env.where(o) == "Other/2026/2026-08-12 document.txt"


def test_file_not_shared_with_the_service_account_is_invisible(env):
    private = env.g.add_folder("Private")  # not shared
    env.g.add_file("secret.txt", private, b"x")
    report = env.filer({}).run()
    assert report.outcomes == []


def test_filer_class_is_importable_without_google_libraries():
    assert Filer.__name__ == "Filer"
