"""List and download the UP Pollution Control Board monthly river water quality reports.

Two places hold them:
- the board's current site (Feb 2024 onward), where each link is labelled with its month;
- Wayback Machine copies of the old uppcb.com/pdf/ folder (2015 - early 2024), unlabelled.
"""

import csv
import html
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

from ganga.config import DATA_CURATED, DATA_RAW
from ganga.ingest.fetch import UA, download, wayback

RAW = DATA_RAW / "uppcb"
MANIFEST = DATA_CURATED / "uppcb_manifest.csv"
SITE = "https://uppcb.up.gov.in/"
PAGES = ["en/page/river-water-quality-data-", "en/article/archive-river-water-quality"]
CDX = (
    "http://web.archive.org/cdx/search/cdx?url=uppcb.com/pdf/&matchType=prefix"
    "&filter=statuscode:200&filter=mimetype:application/pdf&collapse=urlkey"
    "&fl=original,timestamp&limit=5000"
)
_WANT = re.compile(r"rwq|river|nwmp|water", re.I)
_SKIP = re.compile(
    r"PRIORITY|Authorities|Grount|^RIVER-[A-Z()-]+\.pdf$|-River_\d{6}|tamsa|River-Ganga-", re.I
)
_ROW = re.compile(r"<tr.*?</tr>", re.S)
_LABEL = re.compile(r"River Water Quality\s*-\s*([A-Za-z]+)\s+(\d{4})")
_HREF = re.compile(r'href="([^"]*downloadmedia[^"]*)"')


def live_entries(client: httpx.Client) -> list[dict]:
    out = []
    for page in PAGES:
        text = client.get(SITE + page).text
        for row in _ROW.findall(text):
            href = _HREF.search(row)
            label = _LABEL.search(html.unescape(re.sub(r"<[^>]+>", " ", row)))
            if not (href and label):
                continue
            month = datetime.strptime(f"{label.group(1)[:3]} {label.group(2)}", "%b %Y").date()
            path = unquote(href.group(1))
            out.append(
                {
                    "file": f"live_{month:%Y-%m}.pdf",
                    "url": SITE + quote(path),
                    "month": month.isoformat(),
                    "month_source": "site label",
                }
            )
    return out


def archive_entries(client: httpx.Client) -> list[dict]:
    seen, out = set(), []
    for line in client.get(CDX, timeout=180).text.splitlines():
        original, ts = line.split()[:2]
        name = unquote(original.rsplit("/pdf/", 1)[-1]).strip().rstrip("+ ")
        key = name.lower()
        if "/" in name or key in seen or not _WANT.search(name) or _SKIP.search(name):
            continue
        if not name.lower().endswith(".pdf"):
            continue
        seen.add(key)
        out.append(
            {
                "file": "wb_" + re.sub(r"[^A-Za-z0-9._-]", "_", name),
                "url": wayback(original, ts),
                "month": "",
                "month_source": "",
            }
        )
    return out


def build_manifest() -> list[dict]:
    with httpx.Client(headers=UA, timeout=60, follow_redirects=True) as client:
        rows = live_entries(client) + archive_entries(client)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "url", "month", "month_source"])
        w.writeheader()
        w.writerows(rows)
    return rows


def read_manifest() -> list[dict]:
    with MANIFEST.open() as fh:
        return list(csv.DictReader(fh))


def fetch_all(log=print) -> tuple[int, list[str]]:
    rows = read_manifest() if MANIFEST.exists() else build_manifest()
    ok, failed = 0, []
    for r in rows:
        if download(r["url"], RAW / r["file"]):
            ok += 1
        else:
            failed.append(r["file"])
            log(f"failed: {r['file']}")
    return ok, failed


def month_of(row: dict) -> date | None:
    return date.fromisoformat(row["month"]) if row["month"] else None


def path_of(row: dict) -> Path:
    return RAW / row["file"]
