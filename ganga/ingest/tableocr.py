"""OCR for scanned ruled tables.

Whole-page OCR does badly on ruled tables, so we find the grid lines, cut the page into cells,
and read each column as one stacked strip of cells (one tesseract call per column).
"""

import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium

DPI = 300
NUMERIC = "0123456789."
ALNUM = "0123456789.ABCDEabcdeNILnilBDL()-"


def render(pdf_path: Path, dpi: int = DPI) -> list[np.ndarray]:
    """Each page as a grayscale image."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return [
            np.asarray(page.render(scale=dpi / 72, grayscale=True).to_numpy()).squeeze()
            for page in pdf
        ]
    finally:
        pdf.close()


def _ink(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.adaptiveThreshold(
        blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )


def deskew(gray: np.ndarray) -> np.ndarray:
    """Rotate so the table's long horizontal rules are level."""
    ink = _ink(gray)
    h, w = ink.shape
    lines = cv2.HoughLinesP(ink, 1, np.pi / 1800, 200, minLineLength=w // 3, maxLineGap=20)
    if lines is None:
        return gray
    angles = [
        np.degrees(np.arctan2(y2 - y1, x2 - x1))
        for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4)
        if abs(np.degrees(np.arctan2(y2 - y1, x2 - x1))) < 10
    ]
    if not angles:
        return gray
    angle = float(np.median(angles))
    if abs(angle) < 0.05:
        return gray
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)


def _positions(profile: np.ndarray, threshold: float, min_gap: int) -> list[int]:
    """Centres of runs where profile > threshold, merging runs closer than min_gap."""
    idx = np.flatnonzero(profile > threshold)
    if idx.size == 0:
        return []
    groups, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > min_gap:
            groups.append((start + prev) // 2)
            start = i
        prev = i
    groups.append((start + prev) // 2)
    return [int(g) for g in groups]


def find_grid(gray: np.ndarray) -> tuple[list[int], list[int], np.ndarray]:
    """Return (row line ys, column line xs, line mask) for the largest ruled table on the page."""
    ink = _ink(gray)
    h, w = ink.shape
    horiz = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (w // 12, 1))
    )
    vert = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, h // 40))
    )
    ys = _positions(horiz.sum(axis=1) / 255, w * 0.35, 12)
    if len(ys) < 3:
        return [], [], horiz | vert
    # Vertical rules: count only within the table body so header-only rules are ignored.
    body = vert[ys[1] : ys[-1]]
    xs = _positions(body.sum(axis=0) / 255, body.shape[0] * 0.5, 12)
    return ys, xs, cv2.dilate(horiz | vert, np.ones((5, 5), np.uint8))


def _tesseract(img: np.ndarray, whitelist: str, psm: int) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.png"
        cv2.imwrite(str(src), img)
        out = subprocess.run(
            [
                "tesseract",
                str(src),
                "stdout",
                "--psm",
                str(psm),
                "-l",
                "eng",
                "-c",
                f"tessedit_char_whitelist={whitelist}",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    return [ln.strip() for ln in out.splitlines()]


def read_cells(
    gray: np.ndarray, mask: np.ndarray, boxes: list[tuple[int, int, int, int]], whitelist: str
) -> list[str]:
    """OCR a list of cell boxes (y0, y1, x0, x1); one string per cell, '' when blank.

    Cells are stacked into one strip, each on its own line with a marker row count we control,
    then read in a single call. If the line count does not match, fall back to one call per cell.
    """
    clean = gray.copy()
    clean[mask > 0] = 255
    crops, blank = [], []
    for y0, y1, x0, x1 in boxes:
        c = clean[y0 + 3 : y1 - 3, x0 + 3 : x1 - 3]
        dark = (c < 150).sum() if c.size else 0
        blank.append(dark < 25)
        crops.append(c)
    live = [c for c, b in zip(crops, blank, strict=True) if not b]
    texts: list[str] = []
    if live:
        height = 64
        width = max(int(c.shape[1] * height / max(c.shape[0], 1)) for c in live) + 40
        rows = []
        for c in live:
            scale = height / max(c.shape[0], 1)
            r = cv2.resize(
                c, (max(1, int(c.shape[1] * scale)), height), interpolation=cv2.INTER_CUBIC
            )
            pad = np.full((height + 36, width), 255, np.uint8)
            pad[18 : 18 + height, 20 : 20 + r.shape[1]] = r
            rows.append(pad)
        got = [t for t in _tesseract(np.vstack(rows), whitelist, 6) if t]
        if len(got) == len(live):
            texts = got
        else:
            texts = [" ".join(t for t in _tesseract(r, whitelist, 7) if t) for r in rows]
    it = iter(texts)
    return ["" if b else next(it) for b in blank]


def table_cells(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[int], list[int]]:
    """Deskewed page, its line mask, and grid line positions."""
    level = deskew(gray)
    ys, xs, mask = find_grid(level)
    return level, mask, ys, xs
