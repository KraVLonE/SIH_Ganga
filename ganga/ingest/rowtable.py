"""Pull numeric table rows out of a page given positioned text tokens.

Works the same for tokens from a PDF text layer and from OCR. A row is anchored by a known
station code; its values are the numeric tokens on the same line, assigned to value columns by
x position. Columns are found by clustering the x positions of numeric tokens over all rows, so a
blank cell leaves a gap instead of shifting the other values.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

_NUM = re.compile(r"^\d+(?:\.\d+)?$")
_CODE = re.compile(r"^\d{3,5}$")
_CLEAN = re.compile(r"[,\s]")


@dataclass
class Token:
    text: str
    x: float  # centre
    y: float  # centre
    h: float  # height


@dataclass
class Row:
    code: str
    values: list[float | None]  # one per value column, left to right
    y: float


def _number(text: str) -> float | None:
    t = _CLEAN.sub("", text).replace("O", "0").replace("o", "0").strip(".")
    return float(t) if _NUM.match(t) else None


def _cluster(xs: list[float], tol: float) -> list[float]:
    """Centres of 1-D clusters, where neighbours within tol belong together."""
    out: list[list[float]] = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [sum(c) / len(c) for c in out if c]


def extract_rows(
    tokens: list[Token],
    known_codes: set[str],
    n_values: int = 4,
    read_code: Callable[[float, float, float], str | None] | None = None,
) -> list[Row]:
    """Rows for every known station code that has numeric values to its right.

    read_code(x, y, height), if given, is asked to re-read the code cell of a line that has
    values but no recognised code.
    """
    if not tokens:
        return []
    heights = sorted(t.h for t in tokens)
    med_h = heights[len(heights) // 2]
    line_tol = 0.6 * med_h
    anchors = [t for t in tokens if _CODE.match(t.text.strip()) and t.text.strip() in known_codes]
    if not anchors:
        return []
    # Codes sit in one column. A coliform count can equal some station's code, so keep only
    # the anchors in the most populated x cluster.
    centres = _cluster([a.x for a in anchors], 2.2 * med_h)
    code_x = max(centres, key=lambda c: sum(1 for a in anchors if abs(a.x - c) <= 2.2 * med_h))
    anchors = [a for a in anchors if abs(a.x - code_x) <= 2.2 * med_h]
    lines = []
    for a in anchors:
        right = [
            (t, v)
            for t in tokens
            if t.x > a.x and abs(t.y - a.y) <= line_tol and (v := _number(t.text)) is not None
        ]
        lines.append((a, sorted(right, key=lambda tv: tv[0].x)))
    # Value columns: the right-most n_values clusters that are populated on most lines.
    widths = sorted(t.h for t in tokens)
    col_tol = 2.2 * widths[len(widths) // 2]
    centres = _cluster([t.x for _, right in lines for t, _ in right], col_tol)
    counts = [
        sum(1 for _, right in lines if any(abs(t.x - c) <= col_tol for t, _ in right))
        for c in centres
    ]
    need = max(2, int(0.4 * len(lines)))
    cols = [c for c, n in zip(centres, counts, strict=True) if n >= need][-n_values:]
    if len(cols) < n_values:
        return []
    rows = []
    for a, right in lines:
        values: list[float | None] = [None] * n_values
        for t, v in right:
            i = min(range(n_values), key=lambda k: abs(t.x - cols[k]))
            if abs(t.x - cols[i]) <= col_tol and values[i] is None:
                values[i] = v
        if any(v is not None for v in values):
            rows.append(Row(a.text.strip(), values, a.y))
    if read_code is not None:
        rows.extend(
            _orphans(tokens, rows, cols, col_tol, line_tol, code_x, med_h, known_codes, read_code)
        )
    return rows


def _orphans(tokens, rows, cols, col_tol, line_tol, code_x, med_h, known_codes, read_code):
    """Lines with values in most columns but no recognised code: re-read their code cell."""
    n = len(cols)
    taken = [r.y for r in rows]
    have = {r.code for r in rows}
    lines: list[list[tuple[Token, float, int]]] = []
    for t in sorted(tokens, key=lambda t: t.y):
        v = _number(t.text)
        if v is None or t.x <= code_x + col_tol:
            continue
        i = min(range(n), key=lambda k: abs(t.x - cols[k]))
        if abs(t.x - cols[i]) > col_tol:
            continue
        if lines and abs(lines[-1][-1][0].y - t.y) <= line_tol:
            lines[-1].append((t, v, i))
        else:
            lines.append([(t, v, i)])
    out = []
    for line in lines:
        y = sum(t.y for t, _, _ in line) / len(line)
        if len({i for _, _, i in line}) < n - 1 or any(abs(y - ty) <= line_tol for ty in taken):
            continue
        code = read_code(code_x, y, med_h)
        if code and code in known_codes and code not in have:
            values: list[float | None] = [None] * n
            for _, v, i in line:
                if values[i] is None:
                    values[i] = v
            out.append(Row(code, values, y))
            have.add(code)
    return out
