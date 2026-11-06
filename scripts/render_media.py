"""Render the README images headless with Playwright, from the real run output in examples/local-run/.

    python scripts/render_media.py   (needs: pip install playwright && playwright install chromium)

1. media/filing-run.png   the filing run and the folder tree it produced
2. media/find-undo.png    the re-run, three searches and the undo
3. media/sheet-log.png    the Sheet log rows the run wrote
Claude was real in that run; Google Drive and Sheets were the in-memory fake (see the README).
"""
import csv
import html
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "examples" / "local-run"
MEDIA = ROOT / "media"
MONO = "'SF Mono', Menlo, Consolas, monospace"
SANS = "-apple-system, Helvetica, Arial, sans-serif"


def colour(line: str) -> str:
    t = html.escape(line)
    if line.startswith("$"):
        return f"<span style='color:#7ee787'>{t}</span>"
    if ": filed ->" in line or "back to" in line or line.startswith("Run "):
        return f"<span style='color:#3fb950'>{t}</span>"
    if "unsorted" in line or "duplicate ->" in line or "needs a look" in line:
        return f"<span style='color:#d29922'>{t}</span>"
    if line.strip().startswith("made folder"):
        return f"<span style='color:#8b949e'>{t}</span>"
    if line.rstrip().endswith("/"):
        return f"<span style='color:#79c0ff'>{t}</span>"
    return t


def terminal(lines, title):
    body = "\n".join(colour(x) for x in lines)
    return f"""<html><body style="margin:0;background:#0d1117"><div id="wrap" style="display:inline-block;padding:22px 28px;background:#0d1117">
<div style="font-family:{SANS};color:#8b949e;font-size:13px;margin-bottom:10px">{html.escape(title)}</div>
<pre style="margin:0;font-family:{MONO};font-size:13px;line-height:1.45;color:#c9d1d9">{body}</pre></div></body></html>"""


def sheet(rows, title):
    keep = ["status", "original_name", "new_name", "folder", "doc_type", "doc_date", "counterparty", "amount", "currency", "confidence"]
    head = rows[0]
    idx = [head.index(k) for k in keep]
    th = "".join(f"<th>{html.escape(k)}</th>" for k in keep)
    trs = "".join("<tr>" + "".join(f"<td>{html.escape(r[i])}</td>" for i in idx) + "</tr>" for r in rows[1:])
    return f"""<html><body style="margin:0;background:#fff"><div id="wrap" style="display:inline-block;padding:20px 24px;background:#fff;font-family:{SANS}">
<div style="color:#555;font-size:13px;margin-bottom:10px">{html.escape(title)}</div>
<style>table{{border-collapse:collapse;font-size:12px}} th{{background:#f1f3f4;text-align:left}} th,td{{border:1px solid #dadce0;padding:5px 8px;white-space:nowrap}}</style>
<table><tr>{th}</tr>{trs}</table></div></body></html>"""


def main():
    MEDIA.mkdir(exist_ok=True)
    lines = (RUN / "terminal.txt").read_text().splitlines()
    cut = next(i for i, x in enumerate(lines) if x.startswith("$ drive-filer file   (run again)"))
    rows = list(csv.reader((RUN / "sheet-log.csv").read_text().splitlines()))
    pages = [
        (terminal(lines[:cut], "Real run, 6 Nov 2026: Claude Sonnet 5.5 reading 9 sample files (Drive is the in-memory fake)"), "filing-run.png"),
        (terminal(lines[cut:], "Same session: re-run, plain-English search, then undo (0 deletes)"), "find-undo.png"),
        (sheet(rows, "The log rows that run wrote (Sheets API v4 shape, fake Sheet)"), "sheet-log.png"),
    ]
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 1500, "height": 900}, device_scale_factor=2)
        for doc, out in pages:
            page.set_content(doc)
            page.locator("#wrap").screenshot(path=str(MEDIA / out))
            print("wrote media/" + out)
        b.close()


if __name__ == "__main__":
    main()
