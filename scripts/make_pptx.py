"""Build an editable PPTX from docs/deck.html.

Native text boxes, native tables and real pictures, so every slide can be
edited in PowerPoint, Keynote or LibreOffice. One light theme throughout.
"""
from __future__ import annotations

import contextlib
import pathlib
import re
import sys

from bs4 import BeautifulSoup, NavigableString, Tag
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

REPO = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
OUT  = REPO / "dist" / "gujarat-cctv-deck.pptx"

# --- palette -------------------------------------------------------------
# Chart steps were checked with the dataviz validator: CVD separation and the
# normal-vision floor both pass. The light step's sub-3:1 contrast is
# discharged by direct-labelling every bar, which that check requires.
INK      = RGBColor(0x0E, 0x17, 0x21)   # headings
INK2     = RGBColor(0x47, 0x58, 0x6A)   # body
INK3     = RGBColor(0x7A, 0x8A, 0x99)   # captions, kickers
RULE     = RGBColor(0xDC, 0xE3, 0xE8)   # hairlines
RULE2    = RGBColor(0xEE, 0xF2, 0xF5)   # panel fills
PANEL    = RGBColor(0xFF, 0xFF, 0xFF)
GROUND   = RGBColor(0xF7, 0xF9, 0xFA)
PRIMARY  = RGBColor(0x12, 0x50, 0x8F)   # series 1 / structural accent
PRIM_LT  = RGBColor(0x86, 0xB6, 0xE8)   # the tolerance band above series 1
ACCENT   = RGBColor(0xB0, 0x88, 0x00)   # emphasis, used sparingly
GOOD     = RGBColor(0x1F, 0x7A, 0x52)
BAD      = RGBColor(0xB2, 0x3A, 0x2F)

# --- type ----------------------------------------------------------------
# Calibri and Consolas ship with Office and substitute cleanly elsewhere
# (Carlito). The deck's web fonts are not installed on an evaluation machine.
BODY_FONT = "Calibri"
MONO_FONT = "Consolas"

T_DECK    = 40    # slide 1 only
T_TITLE   = 23
T_LEDE    = 13
T_H3      = 12
T_BODY    = 10.5
T_TABLE   = 9
T_TH      = 8
T_NOTE    = 8.5
T_KICKER  = 8.5

# --- geometry ------------------------------------------------------------
W, H   = Inches(13.333), Inches(7.5)
LEFT   = Inches(0.78)
RIGHT  = W - Inches(0.62)
CW     = RIGHT - LEFT
TOP    = Inches(0.58)
FOOT   = Inches(0.46)          # reserved band at the foot of every slide
DECK_NAME = "Gujarat Police Hackathon 2026  ·  Statewide CCTV Integration"

# The dark variant is the same system on an inverted surface, used only at the
# four pivot moments (opening, the constraint, the evidence, the scorecard).
# Scale, accent, rules, footer and spacing are unchanged.
D_SURFACE = RGBColor(0x0E, 0x17, 0x21)
D_INK     = RGBColor(0xFF, 0xFF, 0xFF)
D_INK2    = RGBColor(0xC2, 0xCE, 0xD8)
D_INK3    = RGBColor(0x8A, 0x9B, 0xA8)
D_RULE    = RGBColor(0x2C, 0x3B, 0x49)
D_PANEL   = RGBColor(0x17, 0x22, 0x2E)
D_ACCENT  = RGBColor(0xE8, 0xB9, 0x21)
D_PRIMARY = RGBColor(0x5E, 0xA0, 0xE0)


class Theme:
    """Active surface. Set once per slide; every renderer reads from it."""

    dark = False

    @classmethod
    def use(cls, dark):
        cls.dark = dark

    @classmethod
    def ink(cls):
        return D_INK if cls.dark else INK

    @classmethod
    def ink2(cls):
        return D_INK2 if cls.dark else INK2

    @classmethod
    def ink3(cls):
        return D_INK3 if cls.dark else INK3

    @classmethod
    def rule(cls):
        return D_RULE if cls.dark else RULE

    @classmethod
    def panel(cls):
        return D_PANEL if cls.dark else PANEL

    @classmethod
    def accent(cls):
        return D_ACCENT if cls.dark else ACCENT

    @classmethod
    def primary(cls):
        return D_PRIMARY if cls.dark else PRIMARY

    @classmethod
    def surface(cls):
        return D_SURFACE if cls.dark else PANEL


def txt(node) -> str:
    if node is None:
        return ""
    s = node.get_text(" ", strip=True) if isinstance(node, Tag) else str(node)
    return re.sub(r"\s+", " ", s).replace(" ", " ").strip()


def add_box(slide, x, t, w, h):
    tb = slide.shapes.add_textbox(x, t, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    return tb, tf


def style(run, size, color=INK, bold=False, mono=False, italic=False):
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.font.bold = bold
    run.font.italic = italic
    run.font.name = MONO_FONT if mono else BODY_FONT


def track(run, hundredths):
    """Letter-spacing, in hundredths of a point. Kickers need the air."""
    run.font._rPr.set("spc", str(int(hundredths)))


def rich(par, node, size, color=INK, mono=False):
    """Render inline HTML into one paragraph, keeping bold and colour cues.

    A <br> becomes a real line break inside the paragraph.
    """
    def walk(n, bold=False, col=color):
        if isinstance(n, NavigableString):
            s = re.sub(r"\s+", " ", str(n)).replace(" ", " ")
            if s.strip() or s == " ":
                style(par.add_run(), size, col, bold, mono)
                par.runs[-1].text = s
            return
        if not isinstance(n, Tag):
            return
        if n.name == "br":
            par._p.append(par._p.makeelement(qn("a:br"), {}))
            return
        b = bold or n.name in ("strong", "b", "h3", "th")
        c = col
        cls = n.get("class") or []
        if "good" in cls:
            c = RGBColor(0x5C, 0xB5, 0x8C) if Theme.dark else GOOD
        elif "bad" in cls:
            c = RGBColor(0xE1, 0x76, 0x6A) if Theme.dark else BAD
        elif "y" in cls or "tag" in cls:
            c = Theme.accent()
        elif "num" in cls:
            b = True
        for ch in n.children:
            walk(ch, b, c)
    for ch in node.children:
        walk(ch)
    if not par.runs:
        style(par.add_run(), size, color, mono=mono)
        par.runs[-1].text = txt(node)


# Calibri's average advance width is close to 0.42 em, so a point size buys
# 72/(0.42*size) characters per inch. Arial's wider metrics were over-reserving
# height and leaving a visible gap under every heading.
CPL_CONST = 165


def est_h(text, size, width_in, lh=1.34):
    """Rough wrapped-text height, so stacked blocks do not collide."""
    cpl = max(10, int(width_in * CPL_CONST / size))
    lines = 0
    for para in (text or " ").split("\n"):
        lines += max(1, -(-len(para) // cpl))
    return Inches(lines * size * lh / 72)


# ---------------------------------------------------------------- blocks
def _cell_border(cell, edge, color, pts):
    """python-pptx exposes no border API, so set the line on the cell's XML."""
    tc = cell._tc.get_or_add_tcPr()
    tag = qn(f"a:ln{edge}")
    for old_ln in tc.findall(tag):
        tc.remove(old_ln)
    ln = tc.makeelement(tag, {"w": str(int(pts * 12700)), "cap": "flat",
                              "cmpd": "sng", "algn": "ctr"})
    fill = tc.makeelement(qn("a:solidFill"), {})
    clr = tc.makeelement(qn("a:srgbClr"), {"val": f"{color}"})
    fill.append(clr)
    ln.append(fill)
    tc.append(ln)


def _cell_noline(cell, edge):
    """Clear an inherited table-style border. Only the bottom rule is ours."""
    tc = cell._tc.get_or_add_tcPr()
    tag = qn(f"a:ln{edge}")
    for old_ln in tc.findall(tag):
        tc.remove(old_ln)
    ln = tc.makeelement(tag, {"w": "0"})
    ln.append(tc.makeelement(qn("a:noFill"), {}))
    tc.append(ln)


def _rule_table(tbl, has_head, nrows, ncol):
    """Hairline under every row, a heavier one under the header. No grid."""
    for ri in range(nrows):
        heavy = has_head and ri == 0
        for ci in range(ncol):
            cell = tbl.cell(ri, ci)
            for edge in ("L", "R", "T"):
                _cell_noline(cell, edge)
            hue = ("5EA0E0" if heavy else "2C3B49") if Theme.dark else \
                  ("12508F" if heavy else "DCE3E8")
            _cell_border(cell, "B", hue, 1.0 if heavy else 0.5)


def render_table(slide, el, x, t, w) -> Emu:
    rows = el.find_all("tr")
    ncol = max(len(r.find_all(["td", "th"])) for r in rows)
    has_head = bool(rows[0].find("th"))
    rh = Inches(0.30)
    shape = slide.shapes.add_table(len(rows), ncol, x, t, w, rh * len(rows))
    tbl = shape.table
    # width each column by the text it actually carries, floor 6% of the table
    weights = []
    for ci in range(ncol):
        longest = 0
        for r in rows:
            cs = r.find_all(["td", "th"])
            if ci < len(cs):
                longest = max(longest, len(txt(cs[ci])))
        weights.append(max(longest, 4) ** 0.72)
    total = sum(weights)
    floor = 0.06
    shares = [max(floor, wt / total) for wt in weights]
    shares = [s / sum(shares) for s in shares]
    for ci, s in enumerate(shares):
        tbl.columns[ci].width = Emu(int(w * s))
    tbl.first_row = False          # we draw our own header treatment
    tbl.horz_banding = False
    for ri, r in enumerate(rows):
        cells = r.find_all(["td", "th"])
        for ci in range(ncol):
            cell = tbl.cell(ri, ci)
            cell.margin_left = cell.margin_right = Inches(0.07)
            cell.margin_top = cell.margin_bottom = Inches(0.045)
            cell.vertical_anchor = MSO_ANCHOR.TOP
            cell.fill.solid()
            cell.fill.fore_color.rgb = Theme.surface()
            p_ = cell.text_frame.paragraphs[0]
            cell.text_frame.word_wrap = True
            if ci >= len(cells):
                style(p_.add_run(), T_TABLE, Theme.ink2())
                continue
            src = cells[ci]
            is_h = src.name == "th"
            if is_h:
                style(p_.add_run(), T_TH, Theme.ink3(), True, mono=True)
                p_.runs[-1].text = txt(src).upper()
            else:
                rich(p_, src, T_TABLE, Theme.ink2())
            if "r" in (src.get("class") or []):
                p_.alignment = PP_ALIGN.RIGHT
    _rule_table(tbl, has_head, len(rows), ncol)
    return rh * len(rows)


def render_list(slide, el, x, t, w) -> Emu:
    items = el.find_all("li", recursive=False)
    _, tf = add_box(slide, x, t, w, Inches(0.3))
    total = Inches(0)
    for i, li in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(5)
        style(p.add_run(), T_BODY, Theme.accent(), True)
        p.runs[-1].text = "\u25aa  "
        rich(p, li, T_BODY, Theme.ink2())
        total += est_h(txt(li), T_BODY, w / 914400 - 0.2) + Inches(0.07)
    return total


BOX_CHARS = "─│┌┐└┘├┤┬┴┼"


def declutter_pre(body):
    """Drop the box-drawing frame for PPTX output.

    PowerPoint has no guaranteed monospace box-drawing glyph, so a drawn frame
    renders ragged. The shape behind the text already supplies a border, so the
    frame carries no information here.
    """
    out = []
    for ln in body.split("\n"):
        s = ln.strip()
        if s and all(c in BOX_CHARS + " " for c in s):
            continue
        for c in BOX_CHARS:
            ln = ln.replace(c, " ")
        out.append(ln.rstrip())
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out or [" "]


def render_pre(slide, el, x, t, w) -> Emu:
    body = el.get_text("", strip=False).replace(" ", " ").strip("\n")
    lines = declutter_pre(body)
    box = slide.shapes.add_shape(1, x, t, w, Inches(0.16 * len(lines) + 0.18))
    box.fill.solid()
    box.fill.fore_color.rgb = Theme.panel()
    box.line.color.rgb = Theme.rule()
    box.line.width = Pt(0.75)
    box.shadow.inherit = False
    tf = box.text_frame
    tf.word_wrap = False
    tf.margin_left = tf.margin_right = Inches(0.1)
    tf.margin_top = tf.margin_bottom = Inches(0.07)
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(0)
        p.alignment = PP_ALIGN.LEFT
        style(p.add_run(), 7, Theme.ink2(), mono=True)
        p.runs[-1].text = ln
    return Inches(0.16 * len(lines) + 0.28)


def card_height(el, w) -> Emu:
    tag = el.find("span", class_="tag")
    h3  = el.find("h3")
    ps  = el.find_all("p", recursive=False)
    inner = w / 914400 - 0.34
    h = Inches(0.20)
    if tag:
        h += Inches(0.22)
    if h3:
        h += est_h(txt(h3), T_H3, inner) + Inches(0.05)
    for p_ in ps:
        h += est_h(txt(p_), 9.8, inner) + Inches(0.06)
    return h + Inches(0.22)


def render_card(slide, el, x, t, w, force_h=None) -> Emu:
    tag = el.find("span", class_="tag")
    h3  = el.find("h3")
    ps  = el.find_all("p", recursive=False)
    h = force_h or card_height(el, w)

    box = slide.shapes.add_shape(1, x, t, w, h)
    box.fill.solid()
    box.fill.fore_color.rgb = Theme.panel()
    box.line.color.rgb = Theme.rule()
    box.line.width = Pt(0.75)
    box.shadow.inherit = False
    box.text_frame.word_wrap = True
    tf = box.text_frame
    tf.margin_left = tf.margin_right = Inches(0.17)
    tf.margin_top = tf.margin_bottom = Inches(0.13)
    tf.vertical_anchor = MSO_ANCHOR.TOP
    first = True
    if tag:
        p = tf.paragraphs[0]
        first = False
        p.alignment = PP_ALIGN.LEFT
        style(p.add_run(), 7.5, Theme.accent(), True, mono=True)
        track(p.runs[-1], 60)
        p.runs[-1].text = txt(tag).upper()
        p.space_after = Pt(4)
    if h3:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.alignment = PP_ALIGN.LEFT
        rich(p, h3, T_H3, Theme.ink())
        for r in p.runs:
            r.font.bold = True
        p.space_after = Pt(4)
    for p_ in ps:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.alignment = PP_ALIGN.LEFT
        rich(p, p_, 9.8, Theme.ink2())
        p.space_after = Pt(3)
    return h + Inches(0.14)


def render_quote(slide, el, x, t, w) -> Emu:
    inner = w / 914400 - 0.25
    h = Inches(0.1)
    for p_ in el.find_all("p"):
        h += est_h(txt(p_), T_LEDE, inner) + Inches(0.06)
    bar = slide.shapes.add_shape(1, x, t, Pt(2.5), h)
    bar.fill.solid()
    bar.fill.fore_color.rgb = Theme.accent()
    bar.line.fill.background()
    bar.shadow.inherit = False
    _, tf = add_box(slide, x + Inches(0.16), t, w - Inches(0.16), h)
    for i, p_ in enumerate(el.find_all("p")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        rich(p, p_, T_LEDE, Theme.ink())
        p.space_after = Pt(4)
    return h + Inches(0.12)



# chart series colours: validated with the dataviz palette script (all six
# checks pass for this pair on a light surface)
SERIES = [RGBColor(0x1A, 0x62, 0xB0), RGBColor(0xB0, 0x88, 0x00)]


def render_chart(slide, el, x, t, w, avail) -> Emu:
    """A <figure class="chart"> holding a table becomes a native PPTX chart.

    The table is the single source of truth, so the HTML deck and the PPTX
    cannot drift apart. The chart stays editable: PowerPoint keeps the numbers
    in an embedded worksheet.
    """
    tbl = el.find("table")
    rows = tbl.find_all("tr")
    head = [txt(c) for c in rows[0].find_all(["td", "th"])]
    cats, series = [], [[] for _ in head[1:]]
    for r in rows[1:]:
        cells = r.find_all(["td", "th"])
        cats.append(txt(cells[0]))
        for i, c in enumerate(cells[1:]):
            series[i].append(float(txt(c).rstrip("%")))

    cap = el.find("figcaption")
    cap_h = est_h(txt(cap), T_NOTE, w / 914400) + Inches(0.12) if cap else Inches(0)
    ch_h = min(max(Inches(2.2), avail - cap_h), Inches(3.5))

    data = CategoryChartData()
    data.categories = cats
    for name, vals in zip(head[1:], series, strict=False):
        data.add_series(name, vals)

    gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, t, w, ch_h, data)
    chart = gf.chart
    chart.font.size = Pt(9)
    chart.font.name = BODY_FONT
    chart.font.color.rgb = Theme.ink2()

    chart.has_title = False
    plot = chart.plots[0]
    plot.gap_width = 130          # thin marks: the skill's mark spec
    plot.overlap = -12
    plot.has_data_labels = True          # direct labels: the contrast check requires them
    labels = plot.data_labels
    labels.number_format = '0.0"%"'
    labels.number_format_is_linked = False
    labels.position = XL_LABEL_POSITION.OUTSIDE_END
    labels.font.size = Pt(8.5)
    labels.font.bold = True
    labels.font.color.rgb = Theme.ink()

    for i, s in enumerate(chart.series):
        s.format.fill.solid()
        s.format.fill.fore_color.rgb = SERIES[i % len(SERIES)]
        s.format.line.fill.background()

    if len(series) > 1:
        chart.has_legend = True
        chart.legend.position = XL_LEGEND_POSITION.TOP
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(9)
    else:
        chart.has_legend = False

    # recessive axes: the labels carry the values, the grid should not compete
    va = chart.value_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = Theme.rule()
    va.major_gridlines.format.line.width = Pt(0.5)
    va.has_title = False
    va.tick_labels.font.size = Pt(8)
    va.maximum_scale = 100.0
    va.minimum_scale = 0.0
    ca = chart.category_axis
    ca.has_major_gridlines = False
    ca.tick_labels.font.size = Pt(9)
    ca.format.line.color.rgb = Theme.rule()

    if cap:
        _, tf = add_box(slide, x, t + ch_h + Inches(0.10), w, cap_h)
        rich(tf.paragraphs[0], cap, T_NOTE, Theme.ink3())
    return ch_h + cap_h + Inches(0.10)


def render_figure(slide, el, x, t, w, avail) -> Emu:
    img = el.find("img")
    cap = el.find("figcaption")
    src = (REPO / "docs" / img["src"]).resolve()
    from PIL import Image
    iw, ih = Image.open(src).size
    cap_h = Inches(0)
    if cap:
        cap_h = est_h(txt(cap), T_NOTE, w / 914400) + Inches(0.08)
    box_h = avail - cap_h
    scale = min(w / iw, box_h / ih)
    dw, dh = int(iw * scale), int(ih * scale)
    slide.shapes.add_picture(str(src), x + int((w - dw) / 2), t, dw, dh)
    if cap:
        _, tf = add_box(slide, x, t + dh + Inches(0.08), w, cap_h)
        p = tf.paragraphs[0]
        rich(p, cap, T_NOTE, Theme.ink3())
    return avail


def render_para(slide, el, x, t, w) -> Emu:
    cls = el.get("class") or []
    size, color = (T_BODY, Theme.ink2())
    if "kicker" in cls:
        size, color = T_KICKER, Theme.ink3()
    if "lede" in cls:
        size, color = T_LEDE, Theme.ink2()
    if "note" in cls:
        size, color = T_NOTE, Theme.ink3()
    _, tf = add_box(slide, x, t, w, Inches(0.3))
    p = tf.paragraphs[0]
    rich(p, el, size, color, mono=("kicker" in cls or "note" in cls))
    if "kicker" in cls:
        for r in p.runs:
            r.text = r.text.upper()
            track(r, 90)
    return est_h(txt(el), size, w / 914400) + Inches(0.1)


def render_stats(slide, el, x, t, w) -> Emu:
    stats = el.find_all("div", class_="stat")
    cw = w // max(1, len(stats))
    for i, s in enumerate(stats):
        _, tf = add_box(slide, x + cw * i, t, cw, Inches(0.7))
        p = tf.paragraphs[0]
        style(p.add_run(), 30, Theme.primary(), True)
        p.runs[-1].text = txt(s.find("span", class_="v"))
        p2 = tf.add_paragraph()
        style(p2.add_run(), 7.5, Theme.ink3(), mono=True)
        track(p2.runs[-1], 60)
        p2.runs[-1].text = txt(s.find("span", class_="k")).upper()
    return Inches(0.95)


def render_block(slide, el, x, t, w, avail) -> Emu:
    cls = el.get("class") or []
    if el.name == "table":
        return render_table(slide, el, x, t, w)
    if el.name == "ul":
        return render_list(slide, el, x, t, w)
    if el.name == "pre":
        return render_pre(slide, el, x, t, w)
    if el.name == "figure":
        if "chart" in cls:
            return render_chart(slide, el, x, t, w, avail)
        return render_figure(slide, el, x, t, w, avail)
    if el.name in ("p", "h2", "h3"):
        return render_para(slide, el, x, t, w)
    if "card" in cls:
        return render_card(slide, el, x, t, w)
    if "quote" in cls:
        return render_quote(slide, el, x, t, w)
    if "stats" in cls:
        return render_stats(slide, el, x, t, w)
    if "hr" in cls:
        ln = slide.shapes.add_shape(1, x, t, w, Pt(0.75))
        ln.fill.solid()
        ln.fill.fore_color.rgb = Theme.rule()
        ln.line.fill.background()
        ln.shadow.inherit = False
        return Inches(0.16)
    if "cols" in cls:
        return render_cols(slide, el, x, t, w, avail)
    if el.name == "div":
        return render_stack(slide, el, x, t, w, avail)
    return Inches(0)


def render_stack(slide, parent, x, t, w, avail) -> Emu:
    y = t
    for ch in parent.find_all(recursive=False):
        used = render_block(slide, ch, x, y, w, avail - (y - t))
        y += used + Inches(0.05)
    return y - t


def render_cols(slide, el, x, t, w, avail) -> Emu:
    cls = el.get("class") or []
    kids = el.find_all(recursive=False)
    per = 3 if "c3" in cls else 2 if ("c2" in cls or "c2u" in cls) else max(1, len(kids))
    per = min(per, max(1, len(kids)))
    gap = Inches(0.26)
    cw = (w - gap * (per - 1)) // per

    y = t
    for i in range(0, len(kids), per):
        row = kids[i:i + per]
        # a row made only of cards gets one shared height so the row lines up
        card_row = all((k.get("class") or []) and "card" in (k.get("class") or []) for k in row)
        forced = max((card_height(k, cw) for k in row), default=None) if card_row else None
        tallest = Inches(0)
        for j, k in enumerate(row):
            cx = x + (cw + gap) * j
            kcls = k.get("class") or []
            if "card" in kcls:
                used = render_card(slide, k, cx, y, cw, forced)
            elif kcls or k.name != "div":
                used = render_block(slide, k, cx, y, cw, avail - (y - t))
            else:
                used = render_stack(slide, k, cx, y, cw, avail - (y - t))
            tallest = max(tallest, used)
        y += tallest + Inches(0.16)
    return y - t


# ---------------------------------------------------------------- deck
def build():
    soup = BeautifulSoup((REPO / "docs" / "deck.html").read_text(encoding="utf-8"), "html.parser")
    slides = soup.find_all("section", class_="slide")

    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    blank = prs.slide_layouts[6]

    for idx, sec in enumerate(slides, 1):

        Theme.use("dark" in (sec.get("class") or []))
        slide = prs.slides.add_slide(blank)
        bg = slide.background.fill
        bg.solid()
        bg.fore_color.rgb = Theme.surface()

        body_div = sec.find("div", class_="body")
        is_title = "title-wrap" in (body_div.get("class") or [])
        if is_title:
            # a full-height bar at the very edge: the deck's strongest single
            # visual, and the first thing an evaluator sees
            edge = slide.shapes.add_shape(1, 0, 0, Inches(0.34), H)
            edge.fill.solid()
            edge.fill.fore_color.rgb = Theme.accent()
            edge.line.fill.background()
            edge.shadow.inherit = False

        # --- footer: rule, deck name, element label, slide number ---------
        fy = H - FOOT
        hair = slide.shapes.add_shape(1, LEFT, fy, CW, Pt(0.75))
        hair.fill.solid()
        hair.fill.fore_color.rgb = Theme.rule()
        hair.line.fill.background()
        hair.shadow.inherit = False

        el_lbl = sec.find("span", class_="el")
        foot_left = DECK_NAME
        if el_lbl and not is_title:
            foot_left = f"{txt(el_lbl).upper()}   ·   {DECK_NAME}"
        _, tff = add_box(slide, LEFT, fy + Inches(0.10), CW - Inches(0.8), Inches(0.24))
        style(tff.paragraphs[0].add_run(), 7.5, Theme.ink3(), mono=True)
        tff.paragraphs[0].runs[-1].text = foot_left

        _, tfn = add_box(slide, RIGHT - Inches(0.8), fy + Inches(0.10), Inches(0.8), Inches(0.24))
        tfn.paragraphs[0].alignment = PP_ALIGN.RIGHT
        style(tfn.paragraphs[0].add_run(), 7.5, Theme.ink3(), True, mono=True)
        tfn.paragraphs[0].runs[-1].text = f"{idx:02d}"

        body = sec.find("div", class_="body")
        y = TOP

        kicker = body.find("p", class_="kicker", recursive=False)
        meta = body.find("p", class_="meta", recursive=False)
        if kicker:
            y += render_para(slide, kicker, LEFT, y, CW)
        if meta:
            _, tfm = add_box(slide, LEFT, y, CW, Inches(0.26))
            style(tfm.paragraphs[0].add_run(), T_KICKER, Theme.accent(), True, mono=True)
            track(tfm.paragraphs[0].runs[-1], 90)
            tfm.paragraphs[0].runs[-1].text = txt(meta).upper()
            y += Inches(0.34)

        head = body.find(["h1", "h2"], recursive=False)
        if head:
            size = T_DECK if head.name == "h1" else T_TITLE
            _, tf = add_box(slide, LEFT, y, CW, Inches(0.9))
            rich(tf.paragraphs[0], head, size, Theme.ink())
            for r in tf.paragraphs[0].runs:
                r.font.bold = True
            y += est_h(txt(head), size, CW / 914400) + Inches(0.14)
            # a short accent rule anchors the title and gives the deck a spine
            bar_w = Inches(1.6) if is_title else Inches(0.92)
            bar = slide.shapes.add_shape(1, LEFT, y, bar_w, Pt(3.5 if is_title else 2.75))
            bar.fill.solid()
            bar.fill.fore_color.rgb = Theme.accent()
            bar.line.fill.background()
            bar.shadow.inherit = False
            y += Inches(0.26)

        note = body.find("p", class_="note", recursive=False)
        note_h = Inches(0)
        if note:
            note_h = est_h(txt(note), T_NOTE, CW / 914400) + Inches(0.22)

        avail = H - FOOT - note_h - y - Inches(0.12)
        cursor = y
        first_content = len(slide.shapes)   # to re-centre the block later
        has_figure = bool(body.find("figure"))
        for ch in body.find_all(recursive=False):
            if ch in (kicker, head, note, meta):
                continue
            used = render_block(slide, ch, LEFT, cursor, CW, avail - (cursor - y))
            cursor += used + Inches(0.10)

        # A sparse slide reads better with its content optically centred in the
        # space it has, rather than hugging the title with a void underneath.
        slack = (y + avail) - cursor
        if not has_figure and not is_title and slack > Inches(0.7):
            shift = int(slack * 0.42)
            for sp in list(slide.shapes)[first_content:]:
                with contextlib.suppress(AttributeError, TypeError):
                    sp.top = sp.top + shift

        if note:

            ny = H - FOOT - note_h + Inches(0.06)
            ln = slide.shapes.add_shape(1, LEFT, ny - Inches(0.10), CW, Pt(0.75))
            ln.fill.solid()
            ln.fill.fore_color.rgb = Theme.rule()
            ln.line.fill.background()
            ln.shadow.inherit = False
            render_para(slide, note, LEFT, ny, CW)

    OUT.parent.mkdir(exist_ok=True)
    prs.save(str(OUT))
    print(f"wrote {OUT} ({len(slides)} slides)")


build()
