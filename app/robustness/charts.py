"""Minimal, dependency-free SVG charts for the robustness report.

Output is deterministic (fixed sizes, fixed number formatting), so the same
results always give byte-identical files. Every chart is drawn only from the
values passed in. When there is nothing to draw, ``unavailable`` makes an
explicit "data unavailable" panel instead of an empty or invented chart.
"""

from __future__ import annotations

from html import escape

W, H = 640, 260
PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 34, 46
POS, NEG, LINE, GRID, TEXT = "#2f7d4f", "#b8433a", "#2c5aa0", "#d0d4da", "#333"


def _frame(title: str, body: str, h: int = H) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" '
            f'height="{h}" role="img" aria-label="{escape(title)}" font-family="sans-serif" '
            f'font-size="11"><rect width="{W}" height="{h}" fill="#fff"/>'
            f'<text x="{W / 2:.0f}" y="18" text-anchor="middle" font-size="13" '
            f'fill="{TEXT}">{escape(title)}</text>{body}</svg>\n')


def unavailable(title: str, reason: str) -> str:
    body = (f'<rect x="20" y="40" width="{W - 40}" height="{H - 60}" fill="#f6f6f6" '
            f'stroke="{GRID}"/><text x="{W / 2:.0f}" y="{H / 2:.0f}" text-anchor="middle" '
            f'fill="#666">Data unavailable: {escape(reason)}</text>')
    return _frame(title, body)


def _scale(lo: float, hi: float, a: float, b: float):
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    return lambda v: a + (v - lo) / (hi - lo) * (b - a)


def _axes(ymin: float, ymax: float, sy) -> str:
    out = []
    for i in range(5):
        v = ymin + (ymax - ymin) * i / 4
        y = sy(v)
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" '
                   f'stroke="{GRID}"/><text x="{PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end" '
                   f'fill="{TEXT}">{v:,.2f}</text>')
    return "".join(out)


def line(title: str, labels: list[str], values: list[float], zero_line: bool = True) -> str:
    if len(values) < 2:
        return unavailable(title, "fewer than two points")
    ymin, ymax = min(values + [0.0] if zero_line else values), max(values + [0.0]
                                                                   if zero_line else values)
    sx = _scale(0, len(values) - 1, PAD_L, W - PAD_R)
    sy = _scale(ymin, ymax, H - PAD_B, PAD_T)
    pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(values))
    ticks = "".join(f'<text x="{sx(i):.1f}" y="{H - PAD_B + 16}" text-anchor="middle" '
                    f'fill="{TEXT}">{escape(labels[i])}</text>'
                    for i in sorted({0, len(values) // 2, len(values) - 1}))
    body = _axes(ymin, ymax, sy) + ticks + \
        f'<polyline fill="none" stroke="{LINE}" stroke-width="1.6" points="{pts}"/>'
    return _frame(title, body)


def bars(title: str, labels: list[str], values: list[float | None],
         notes: list[str] | None = None) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return unavailable(title, "no values")
    ymin, ymax = min(present + [0.0]), max(present + [0.0])
    sy = _scale(ymin, ymax, H - PAD_B, PAD_T)
    n = len(values)
    slot = (W - PAD_L - PAD_R) / n
    out = [_axes(ymin, ymax, sy)]
    for i, (lbl, v) in enumerate(zip(labels, values)):
        x = PAD_L + i * slot + slot * 0.15
        w = slot * 0.7
        if v is None:
            out.append(f'<text x="{x + w / 2:.1f}" y="{sy(0) - 4:.1f}" text-anchor="middle" '
                       f'fill="#888">n/a</text>')
        else:
            y0, y1 = sorted((sy(0), sy(v)))
            out.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{max(y1 - y0, 0.5):.1f}" '
                       f'fill="{POS if v >= 0 else NEG}"/>')
        out.append(f'<text x="{x + w / 2:.1f}" y="{H - PAD_B + 14}" text-anchor="middle" '
                   f'fill="{TEXT}">{escape(lbl)}</text>')
        if notes:
            out.append(f'<text x="{x + w / 2:.1f}" y="{H - PAD_B + 28}" text-anchor="middle" '
                       f'fill="#777">{escape(notes[i])}</text>')
    return _frame(title, "".join(out))


def heatmap(title: str, x_label: str, y_label: str, xs: list, ys: list,
            values: dict[tuple, float | None], counts: dict[tuple, int | None]) -> str:
    present = [v for v in values.values() if v is not None]
    if not present:
        return unavailable(title, "no configuration produced out-of-sample trades")
    h = 60 + 36 * len(ys) + 40
    cw = (W - 140) / len(xs)
    m = max(abs(v) for v in present) or 1.0
    out = []
    for j, y in enumerate(ys):
        yy = 40 + j * 36
        out.append(f'<text x="120" y="{yy + 22}" text-anchor="end" fill="{TEXT}">'
                   f'{escape(str(y))}</text>')
        for i, x in enumerate(xs):
            v = values.get((x, y))
            xx = 130 + i * cw
            if v is None:
                fill, txt = "#eeeeee", "n/a"
            else:
                a = min(abs(v) / m, 1.0)
                base = (47, 125, 79) if v >= 0 else (184, 67, 58)
                rgb = tuple(int(255 - (255 - c) * (0.15 + 0.85 * a)) for c in base)
                fill, txt = f"rgb{rgb}", f"{v:,.2f}"
            n = counts.get((x, y))
            out.append(f'<rect x="{xx:.1f}" y="{yy}" width="{cw - 2:.1f}" height="34" '
                       f'fill="{fill}"/><text x="{xx + cw / 2:.1f}" y="{yy + 16}" '
                       f'text-anchor="middle" fill="{TEXT}">{txt}</text>'
                       f'<text x="{xx + cw / 2:.1f}" y="{yy + 29}" text-anchor="middle" '
                       f'fill="#555">n={n if n is not None else "-"}</text>')
    for i, x in enumerate(xs):
        out.append(f'<text x="{130 + i * cw + cw / 2:.1f}" y="{40 + len(ys) * 36 + 16}" '
                   f'text-anchor="middle" fill="{TEXT}">{escape(str(x))}</text>')
    out.append(f'<text x="{W / 2:.0f}" y="{h - 8}" text-anchor="middle" fill="{TEXT}">'
               f'{escape(x_label)} (rows: {escape(y_label)})</text>')
    return _frame(title, "".join(out), h)
