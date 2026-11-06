"""Turn a downloaded file's bytes into text Claude can read.

Adapted from the loaders in ParishSynthMind/ai-document-extractor (same rules:
a PDF's text layer first, Tesseract OCR for any page with almost no text),
changed to work on bytes straight from Drive, and extended with images
(PNG/JPG through OCR) and plain text (what Google Docs export to).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

SUPPORTED = {".pdf", ".docx", ".xlsx", ".csv", ".txt", ".png", ".jpg", ".jpeg"}
MIN_TEXT_CHARS = 25  # a PDF page with fewer non-space characters is treated as scanned
OCR_DPI = 300
MAX_CHARS = 60_000   # a filing decision never needs more than the first ~15 pages of text


class UnsupportedFile(ValueError):
    pass


@dataclass
class LoadedDocument:
    name: str
    text: str
    method: str
    pages: int = 1
    ocr_pages: list[int] = field(default_factory=list)


def ocr_image(image) -> str:
    import pytesseract  # needs the tesseract binary on PATH

    return pytesseract.image_to_string(image.convert("L"), config="--psm 4")


def load_bytes(data: bytes, ext: str, name: str = "", ocr=ocr_image) -> LoadedDocument:
    ext = ext.lower()
    if ext not in SUPPORTED:
        raise UnsupportedFile(f"{name or 'file'}: unsupported type {ext or '(none)'}")
    if ext == ".pdf":
        doc = _pdf(data, name, ocr)
    elif ext == ".docx":
        doc = _docx(data, name)
    elif ext == ".xlsx":
        doc = _xlsx(data, name)
    elif ext == ".csv":
        doc = _csv(data, name)
    elif ext == ".txt":
        doc = LoadedDocument(name, data.decode("utf-8-sig", errors="replace"), "text")
    else:
        from PIL import Image

        doc = LoadedDocument(name, ocr(Image.open(io.BytesIO(data))), "image-ocr", 1, [1])
    doc.text = doc.text[:MAX_CHARS]
    return doc


def _pdf(data: bytes, name: str, ocr) -> LoadedDocument:
    import pdfplumber

    chunks, ocr_pages = [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if len("".join(text.split())) < MIN_TEXT_CHARS:
                text = ocr(page.to_image(resolution=OCR_DPI).original)
                ocr_pages.append(i)
            chunks.append(f"--- page {i} ---\n{text.strip()}")
        pages = len(pdf.pages)
    method = "pdf-text" if not ocr_pages else ("ocr" if len(ocr_pages) == pages else "pdf-text+ocr")
    return LoadedDocument(name, "\n\n".join(chunks), method, pages, ocr_pages)


def _docx(data: bytes, name: str) -> LoadedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    lines = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, d).text
            if text.strip():
                lines.append(text)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells = [c.text.strip() for c in row.cells]
                lines.append(" | ".join(c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]))
    return LoadedDocument(name, "\n".join(lines), "docx")


def _xlsx(data: bytes, name: str) -> LoadedDocument:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"--- sheet: {ws.title} ---")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells).rstrip(" |"))
    pages = len(wb.worksheets)
    wb.close()
    return LoadedDocument(name, "\n".join(lines), "xlsx", pages)


def _csv(data: bytes, name: str) -> LoadedDocument:
    rows = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    return LoadedDocument(name, "\n".join(" | ".join(r) for r in rows if any(c.strip() for c in r)), "csv")
