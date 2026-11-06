"""Where a document goes and what it is called, decided in plain code from Claude's checked reading.

The rules live in config/filer.yaml so a client can change folder layout or
naming without touching code. Claude never picks a folder path itself: it
only supplies fields, and this module turns confident fields into a path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .classify import LEVELS, Reading

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "filer.yaml"
TYPE_LABELS = {"invoice": "invoice", "receipt": "receipt", "contract": "contract",
               "bank_statement": "bank statement", "other": "document"}
COMPANY_SUFFIXES = r"(,?\s+(ltd\.?|limited|llc|l\.l\.c\.|inc\.?|incorporated|plc|llp|gmbh|corp\.?|corporation|co\.)$)"
BAD_CHARS = r'[\\/:*?"<>|\x00-\x1f]'
MAX_NAME = 120


@dataclass
class Rules:
    folders: dict
    unsorted: str = "Unsorted"
    duplicates: str = "Duplicates"
    min_confidence: str = "medium"
    required: dict = field(default_factory=dict)
    cost_cap_usd: float = 0.50

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Rules":
        data = yaml.safe_load(Path(path or DEFAULT_CONFIG).read_text(encoding="utf-8"))
        rules = cls(**data)
        if rules.min_confidence not in LEVELS:
            raise ValueError(f"min_confidence must be one of {LEVELS}")
        missing = set(TYPE_LABELS) - set(rules.folders)
        if missing:
            raise ValueError(f"folders: no rule for {sorted(missing)}")
        return rules


@dataclass
class Plan:
    status: str                 # "filed" or "unsorted"
    folder: list[str]           # path parts under the root folder, e.g. ["Invoices", "2026", "10"]
    new_name: str
    reasons: list[str] = field(default_factory=list)


def short_counterparty(name: str) -> str:
    name = clean(name)
    shorter = re.sub(COMPANY_SUFFIXES, "", name, flags=re.IGNORECASE).strip(" ,")
    return shorter or name


def clean(text: str) -> str:
    return " ".join(re.sub(BAD_CHARS, " ", str(text)).split())


def build_name(reading: Reading, ext: str) -> str:
    doc_type = reading.value("doc_type") or "other"
    parts = [str(reading.value("doc_date"))]
    if reading.value("counterparty"):
        parts.append(short_counterparty(reading.value("counterparty")))
    parts.append(TYPE_LABELS.get(doc_type, "document"))
    if reading.value("amount") is not None and reading.value("currency"):
        parts.append(f"{float(reading.value('amount')):.2f} {reading.value('currency')}")
    stem = clean(" ".join(parts))[: MAX_NAME - len(ext)].rstrip()
    return stem + ext


def _ok(reading: Reading, name: str, floor: str) -> bool:
    return reading.value(name) is not None and LEVELS.index(reading.conf(name)) >= LEVELS.index(floor)


def plan(reading: Reading, original_name: str, ext: str, rules: Rules) -> Plan:
    """ext is the original extension to keep ('' for Google Docs/Sheets, whose names carry none)."""
    doc_type = reading.value("doc_type")
    reasons = []
    if not _ok(reading, "doc_type", rules.min_confidence):
        reasons.append(f"document type unclear ({reading.conf('doc_type')})")
    else:
        for name in rules.required.get(doc_type, []):
            if not _ok(reading, name, rules.min_confidence):
                fr = reading.fields[name]
                why = "; ".join(fr.checks) or ("missing" if fr.value is None else f"{fr.confidence} confidence")
                reasons.append(f"{name}: {why}")
    if reasons:
        return Plan("unsorted", [rules.unsorted], original_name, reasons)
    d = str(reading.value("doc_date") or "")
    yyyy, mm = (d[:4], d[5:7]) if len(d) >= 7 else ("undated", "")
    path = rules.folders[doc_type].format(yyyy=yyyy, mm=mm)
    folder = [clean(p) for p in path.split("/") if clean(p)]
    name = build_name(reading, ext) if reading.value("doc_date") else original_name
    return Plan("filed", folder, name)


def with_suffix_number(name: str, taken: set[str]) -> str:
    """'x.pdf' -> 'x (2).pdf' if a different file already has that name in the folder."""
    if name not in taken:
        return name
    dot = name.rfind(".")
    stem, ext = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    n = 2
    while f"{stem} ({n}){ext}" in taken:
        n += 1
    return f"{stem} ({n}){ext}"
