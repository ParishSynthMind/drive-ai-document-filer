"""find "invoices from Brightwater in October": plain-English search over what the filer stored.

The question is turned into filters by simple rules (no model call, so it is
free and predictable): a document type, a counterparty after "from"/"with",
a month and/or year, and an optional amount bound. Exact fields go to Drive
as appProperties filters; the counterparty goes to Drive's full-text search
(which covers the description the filer wrote) and is then checked against
the stored counterparty, so "Brightwater" finds "Brightwater Marine Supplies".
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .drive import Drive, DriveFile, q_escape
from .filer import TOOL, slug

TYPE_WORDS = {
    "invoice": "invoice", "invoices": "invoice", "bill": "invoice", "bills": "invoice",
    "receipt": "receipt", "receipts": "receipt",
    "contract": "contract", "contracts": "contract", "agreement": "contract", "agreements": "contract",
    "statement": "bank_statement", "statements": "bank_statement",
}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
STOP = r"(?=\s+(?:in|during|for|from|over|under|above|below|more|less|since|last|this|of|dated)\b|\s*$)"


@dataclass
class Query:
    doc_type: Optional[str] = None
    counterparty: Optional[str] = None
    period: Optional[str] = None   # YYYY-MM
    year: Optional[str] = None
    min_amount: Optional[float] = None
    max_amount: Optional[float] = None
    currency: Optional[str] = None


def parse(text: str, today: Optional[date] = None) -> Query:
    today = today or date.today()
    t = " " + text.lower().strip() + " "
    q = Query()
    for word in re.findall(r"[a-z]+", t):
        if word in TYPE_WORDS:
            q.doc_type = TYPE_WORDS[word]
            break
    m = re.search(r"\b(?:from|with|by)\s+(.+?)" + STOP, t)
    if m:
        name = re.sub(r"\b(" + "|".join(MONTHS) + r")\b.*$", "", m.group(1)).strip(" ,")
        q.counterparty = name or None
    if re.search(r"\blast month\b", t):
        y, mo = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        q.period = f"{y}-{mo:02d}"
    elif re.search(r"\bthis month\b", t):
        q.period = f"{today.year}-{today.month:02d}"
    else:
        mm = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?(?:\s+(\d{4}))?", t)
        if mm:
            month = MONTHS[mm.group(1)]
            year = int(mm.group(2)) if mm.group(2) else (today.year if month <= today.month else today.year - 1)
            q.period = f"{year}-{month:02d}"
    if q.period is None:
        if re.search(r"\blast year\b", t):
            q.year = str(today.year - 1)
        elif re.search(r"\bthis year\b", t):
            q.year = str(today.year)
        else:
            y = re.search(r"\b(19|20)\d{2}\b", t)
            if y:
                q.year = y.group(0)
    lo = re.search(r"\b(?:over|above|more than|at least)\s+[£$€]?\s*([\d,]+(?:\.\d+)?)", t)
    hi = re.search(r"\b(?:under|below|less than|at most)\s+[£$€]?\s*([\d,]+(?:\.\d+)?)", t)
    if lo:
        q.min_amount = float(lo.group(1).replace(",", ""))
    if hi:
        q.max_amount = float(hi.group(1).replace(",", ""))
    cur = re.search(r"\b(gbp|usd|eur|aed)\b", t)
    if cur:
        q.currency = cur.group(1).upper()
    return q


def drive_query(q: Query) -> tuple[dict, str]:
    props = {"filer": TOOL}
    if q.doc_type:
        props["doc_type"] = q.doc_type
    if q.period:
        props["period"] = q.period
    elif q.year:
        props["year"] = q.year
    if q.currency:
        props["currency"] = q.currency
    extra = f"fullText contains '{q_escape(q.counterparty)}'" if q.counterparty else ""
    return props, extra


def matches(f: DriveFile, q: Query) -> bool:
    p = f.app_properties
    if q.counterparty and slug(q.counterparty) not in p.get("counterparty", ""):
        return False
    try:
        amount = float(p["amount"]) if "amount" in p else None
    except ValueError:
        amount = None
    if q.min_amount is not None and (amount is None or amount < q.min_amount):
        return False
    if q.max_amount is not None and (amount is None or amount > q.max_amount):
        return False
    return True


def find(drive: Drive, text: str, today: Optional[date] = None) -> tuple[Query, list[DriveFile]]:
    q = parse(text, today)
    props, extra = drive_query(q)
    results = [f for f in drive.find_by_app_properties(props, extra) if matches(f, q)]
    results.sort(key=lambda f: f.app_properties.get("doc_date", ""))
    return q, results
