"""Claude's reply checking, the cost cap, and the folder/name rules."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import raw

from drive_filer.classify import (
    ClaudeReader,
    CostCapReached,
    CostTracker,
    Reading,
    amount_in_document,
    check_reading,
    json_schema,
    parse_number,
)
from drive_filer.rules import Rules, build_name, clean, plan, short_counterparty, with_suffix_number

DOC = "TAX INVOICE\nBrightwater Office Supplies Ltd\nInvoice BW-2026-0412\nDate 2026-10-14\nTotal GBP 1,234.50\n"


def reading(reply, text=DOC):
    return Reading(check_reading(reply, text))


# --- checks ---------------------------------------------------------------
def test_good_reply_keeps_its_confidence():
    r = reading(raw(amount=1234.5, fields={"amount": {"source_snippet": "1,234.50"}}))
    assert all(f.confidence == "high" for f in r.fields.values())


def test_snippet_not_in_document_drops_to_low():
    r = reading(raw(fields={"reference": {"source_snippet": "INV-999"}}))
    assert r.conf("reference") == "low"
    assert "source snippet not found" in r.fields["reference"].checks[0]


def test_counterparty_not_in_document_is_marked_down():
    r = reading(raw(counterparty="Acme Corp", fields={"counterparty": {"source_snippet": "Brightwater Office"}}))
    assert r.conf("counterparty") == "medium"


def test_bad_date_is_low():
    r = reading(raw(doc_date="14/10/2026", fields={"doc_date": {"source_snippet": "2026-10-14"}}))
    assert r.conf("doc_date") == "low"


def test_amount_must_appear_in_the_document():
    r = reading(raw(amount=999.0, fields={"amount": {"source_snippet": "1,234.50"}}))
    assert r.conf("amount") == "medium"
    assert "amount not found" in r.fields["amount"].checks[0]


@pytest.mark.parametrize("text,amount", [("Total 627.00", 627), ("Total 1,234.50", 1234.5), ("Summe 1.234,50", 1234.5),
                                         ("Total 1 234,50", 1234.5), ("AED 86.50", 86.5), ("Total: 627", 627.0)])
def test_amount_forms(text, amount):
    assert amount_in_document(amount, text)


def test_amount_not_confused_with_other_numbers():
    assert not amount_in_document(627, "Invoice 6270, date 2026-10-14")


def test_parse_number():
    assert parse_number("1,234") == 1234 and parse_number("12,50") == 12.5 and parse_number("1.234.567") == 1234567


def test_currency_is_normalised_and_checked():
    assert reading(raw(currency="gbp", fields={"currency": {"source_snippet": "GBP"}})).value("currency") == "GBP"
    assert reading(raw(currency="pounds", fields={"currency": {"source_snippet": "GBP"}})).conf("currency") == "low"


def test_missing_fields_in_reply_are_low_and_empty():
    r = reading({})
    assert r.value("doc_type") is None and r.conf("doc_type") == "low"


def test_unknown_confidence_word_becomes_low():
    assert reading(raw(conf="certain")).conf("doc_type") == "low"


def test_schema_offers_only_known_types():
    s = json_schema()
    assert s["properties"]["doc_type"]["properties"]["value"]["anyOf"][0]["enum"] == \
        ["invoice", "receipt", "contract", "bank_statement", "other"]
    assert s["additionalProperties"] is False


# --- cost -----------------------------------------------------------------
def test_cost_uses_sonnet_prices():
    t = CostTracker(1.0)
    assert t.add(1_000_000, 100_000) == pytest.approx(3.0)


def test_cost_cap_blocks_before_the_call():
    t = CostTracker(0.01)
    with pytest.raises(CostCapReached):
        t.check("x" * 10_000)


# --- the real reader with a fake Anthropic client ---------------------------
class FakeMessages:
    def __init__(self, reply, stop="end_turn"):
        self.reply, self.stop, self.kwargs = reply, stop, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason=self.stop, usage=SimpleNamespace(input_tokens=2000, output_tokens=300),
                               content=[SimpleNamespace(type="text", text=json.dumps(self.reply))])


def fake_client(reply, stop="end_turn"):
    msgs = FakeMessages(reply, stop)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_reader_sends_document_as_data_and_costs_the_call():
    c, msgs = fake_client(raw(amount=1234.5, fields={"amount": {"source_snippet": "1,234.50"}}))
    costs = CostTracker(1.0)
    r = ClaudeReader(client=c).read(DOC, "scan.pdf", costs)
    assert r.value("counterparty") == "Brightwater Office Supplies Ltd"
    assert r.cost_usd == pytest.approx(0.007)
    assert costs.calls == 1
    assert "<document>" in msgs.kwargs["messages"][0]["content"]
    assert "data, not instructions" in msgs.kwargs["system"]
    assert msgs.kwargs["model"] == "claude-sonnet-5-5"
    assert msgs.kwargs["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.parametrize("stop,msg", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_reader_stop_reasons_raise(stop, msg):
    c, _ = fake_client(raw(), stop)
    with pytest.raises(RuntimeError, match=msg):
        ClaudeReader(client=c).read(DOC, "x", CostTracker(1.0))


# --- rules ----------------------------------------------------------------
RULES = Rules.load()


def test_name_matches_the_convention():
    r = reading(raw(amount=1234.5, fields={"amount": {"source_snippet": "1,234.50"}}))
    assert build_name(r, ".pdf") == "2026-10-14 Brightwater Office Supplies invoice 1234.50 GBP.pdf"


@pytest.mark.parametrize("name,short", [("Brightwater Office Supplies Ltd", "Brightwater Office Supplies"),
                                        ("KESTREL MARINE SERVICES LLC", "KESTREL MARINE SERVICES"),
                                        ("Oakfield Catering Co.", "Oakfield Catering"),
                                        ("Acme, Inc.", "Acme"), ("Ltd", "Ltd")])
def test_company_suffixes_are_dropped(name, short):
    assert short_counterparty(name) == short


def test_unsafe_characters_are_removed_from_names():
    assert clean('A/B: "C" <D>|E?*') == "A B C D E"


def test_plan_files_confident_invoice():
    p = plan(reading(raw(amount=1234.5, fields={"amount": {"source_snippet": "1,234.50"}})), "scan.pdf", ".pdf", RULES)
    assert p.status == "filed" and p.folder == ["Invoices", "2026", "10"]


def test_plan_unclear_type_is_unsorted():
    p = plan(reading(raw(fields={"doc_type": {"confidence": "low"}})), "scan.pdf", ".pdf", RULES)
    assert p.status == "unsorted" and p.new_name == "scan.pdf" and "type unclear" in p.reasons[0]


def test_plan_lists_every_reason():
    p = plan(reading(raw(counterparty=None, amount=None)), "scan.pdf", ".pdf", RULES)
    assert len(p.reasons) == 2 and p.reasons[0].startswith("counterparty")


def test_contract_needs_no_amount():
    text = "SERVICES AGREEMENT dated 2026-09-01 with Northwind Data Services Limited"
    r = reading(raw("contract", "2026-09-01", "Northwind Data Services Limited", None, None, None,
                    fields={"doc_type": {"source_snippet": "SERVICES AGREEMENT"}}), text)
    p = plan(r, "agreement.docx", ".docx", RULES)
    assert p.folder == ["Contracts", "2026"] and p.new_name == "2026-09-01 Northwind Data Services contract.docx"


def test_stricter_confidence_setting(tmp_path):
    cfg = tmp_path / "strict.yaml"
    cfg.write_text(open(Rules.load.__globals__["DEFAULT_CONFIG"]).read().replace("min_confidence: medium",
                                                                               "min_confidence: high"))
    strict = Rules.load(str(cfg))
    r = reading(raw(fields={"counterparty": {"confidence": "medium"}}))
    assert plan(r, "x.pdf", ".pdf", strict).status == "unsorted"


def test_bad_config_is_refused(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("folders: {invoice: Invoices}\nmin_confidence: medium\n")
    with pytest.raises(ValueError, match="no rule"):
        Rules.load(str(cfg))


def test_suffix_numbers():
    assert with_suffix_number("a.pdf", {"a.pdf", "a (2).pdf"}) == "a (3).pdf"
    assert with_suffix_number("Doc", {"Doc"}) == "Doc (2)"
    assert with_suffix_number("a.pdf", set()) == "a.pdf"
